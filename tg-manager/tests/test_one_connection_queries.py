"""Пачка запросов по одному соединению отдаёт ВСЕ результаты, а не первый.

Здесь был «ускоритель»: asyncio.gather по одному соединению из пула. Соединение
asyncpg так не умеет — пока идёт один запрос, второй получает
`InterfaceError: cannot perform operation: another operation is in progress`.
Ошибку глотал `except Exception: return []`, поэтому первый запрос отдавал
данные, а остальные — пустоту. Молча и всегда.

Что из-за этого было сломано у владельца:

* `get_audience_stats` — одно число из шести, остальные нули;
* `get_bot_stats` — одно из шестнадцати;
* дашборд приложения (`analytics_dashboard`) не показывал нули, а ПАДАЛ: он
  берёт первую строку результата (`ops[0]["total"]`), а результат был пустым
  списком.

Проверено на живой Postgres (см. test_real_asyncpg_connection_refuses_overlap
в tests/test_miniapp_bulk_batched_e2e_postgres.py).
"""
from __future__ import annotations

import asyncio

import pytest


class _AsyncpgLikeConn:
    """Соединение, ведущее себя как настоящее: один запрос в момент времени."""

    def __init__(self):
        self.busy = False
        self.seen: list[str] = []

    async def fetch(self, sql, *params):
        if self.busy:
            raise RuntimeError(
                "cannot perform operation: another operation is in progress")
        self.busy = True
        try:
            self.seen.append(sql)
            await asyncio.sleep(0)      # точка переключения, как настоящий ввод-вывод
            return [{"sql": sql, "params": params}]
        finally:
            self.busy = False


class _Pool:
    def __init__(self, conn=None, broken_sql: str | None = None):
        self.conn = conn or _AsyncpgLikeConn()
        self.broken_sql = broken_sql
        self.acquires = 0
        if broken_sql:
            real = self.conn.fetch

            async def fetch(sql, *params):
                if sql == broken_sql:
                    raise RuntimeError('relation "нет_такой" does not exist')
                return await real(sql, *params)

            self.conn.fetch = fetch

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.acquires += 1
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


QUERIES = [(f"SELECT {i}", (i,)) for i in range(6)]


def _run(pool, queries=QUERIES):
    from database.db import run_queries_on_one_connection

    return asyncio.run(run_queries_on_one_connection(pool, queries))


def test_every_query_comes_back():
    """Главное: шесть запросов — шесть результатов, а не один и пять пустот."""
    pool = _Pool()
    res = _run(pool)
    assert len(res) == 6
    empty = [i for i, r in enumerate(res) if not r]
    assert not empty, (
        f"пустые результаты у запросов {empty} — запросы шли внахлёст по одному "
        f"соединению")


def test_order_is_preserved():
    """Вызывающие разбирают результат позиционно (acc, ops, running, ...)."""
    res = _run(_Pool())
    assert [r[0]["sql"] for r in res] == [q[0] for q in QUERIES]


def test_connection_is_taken_from_the_pool_once():
    """Смысл помощника — одно взятие соединения вместо шести."""
    pool = _Pool()
    _run(pool)
    assert pool.acquires == 1


def test_one_broken_query_does_not_empty_the_rest():
    """Отсутствующая таблица в одном запросе не должна обнулять весь набор."""
    pool = _Pool(broken_sql="SELECT 3")
    res = _run(pool)
    assert res[3] == []
    assert all(res[i] for i in (0, 1, 2, 4, 5)), "соседи пострадали от одного сбоя"


def test_empty_list_of_queries_is_fine():
    assert _run(_Pool(), []) == []


def test_no_gather_over_a_single_connection_anywhere():
    """Храповик: вторая копия этого «ускорителя» не должна появиться снова.

    Ищем `asyncio.gather` внутри блока `async with ...acquire()`: именно такая
    форма и ломалась. Границы блока берём из AST, а не окном по строкам.
    """
    import ast
    import glob

    offenders = []
    for path in sorted(glob.glob("services/*.py") + glob.glob("database/*.py")):
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue
            if not any(".acquire(" in ast.unparse(i.context_expr) for i in node.items):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and ast.unparse(sub.func).endswith(
                        "asyncio.gather"):
                    offenders.append(f"{path}:{sub.lineno}")
    assert not offenders, (
        "asyncio.gather по одному соединению из пула: соединение asyncpg "
        "выполняет один запрос в момент времени, остальные падают с "
        "InterfaceError. Идите в ряд: " + ", ".join(offenders))


def test_audience_stats_returns_all_six_numbers():
    """Сквозная проверка вызывающего: раньше пять чисел из шести были нулями."""
    from database import db

    class _Counting(_AsyncpgLikeConn):
        async def fetch(self, sql, *params):
            await super().fetch(sql, *params)
            if "language_code" in sql:
                return [{"lang": "ru", "cnt": 7}]
            return [{"count": 42}]

    pool = _Pool(conn=_Counting())
    res = asyncio.run(db.get_audience_stats(pool, 111))
    assert res["total"] == 42
    for field in ("inactive", "joined_today", "joined_week", "joined_month"):
        assert res[field] == 42, f"{field} обнулилось: {res}"
    assert res["languages"] == [{"lang": "ru", "count": 7}]
