"""Храповик: внутри `conn.transaction()` ошибку запроса нельзя «проглотить».

ПОЧЕМУ. В Postgres упавший запрос ломает всю транзакцию: следующие запросы
падают с InFailedSQLTransactionError, а COMMIT молча становится ROLLBACK.
Значит `try: await conn.execute(...) except Exception: pass` внутри
транзакции — не «некритично», а тихий откат всего, что она успела сделать.
Так терялись: включение оплаченной подписки (`billing.activate_subscription`),
счётчик переходов по ссылке (`db.record_deep_link_visit`), подписка на
воронку (`db.subscribe_to_funnel`), восстановление бэкапа целиком
(`db_backup`, одна отсутствующая секвенция).

КАК ПРАВИЛЬНО. Либо `ON CONFLICT`/проверка вместо ожидаемой ошибки, либо
точка сохранения — вложенный `async with conn.transaction():` вокруг
необязательного запроса, либо `raise` в обработчике.

Проверка — по дереву разбора: обработчик исключения, который ничего не
поднимает, у `try`, внутри которого есть вызов на том же соединении, что и
открытая транзакция, а точки сохранения нет.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIRS = ("services", "bot", "database")


def _tx_conn(node) -> str | None:
    for item in node.items:
        c = item.context_expr
        if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "transaction"):
            return ast.unparse(c.func.value)
    return None


def _offenders(tree: ast.AST, path: str) -> list[str]:
    out = []
    for w in ast.walk(tree):
        if not isinstance(w, (ast.AsyncWith, ast.With)):
            continue
        conn = _tx_conn(w)
        if conn is None:
            continue
        for tr in ast.walk(w):
            if not isinstance(tr, ast.Try):
                continue
            body_nodes = [x for b in tr.body for x in ast.walk(b)]
            if any(isinstance(x, (ast.AsyncWith, ast.With)) and _tx_conn(x) is not None
                   for x in body_nodes):
                continue  # точка сохранения
            if not any(isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                       and ast.unparse(x.func.value) == conn for x in body_nodes):
                continue
            for h in tr.handlers:
                if any(isinstance(x, ast.Raise) for b in h.body for x in ast.walk(b)):
                    continue
                out.append(f"{path}:{tr.lineno} (соединение {conn})")
    return out


def test_no_swallowed_error_inside_a_transaction():
    found: list[str] = []
    for d in DIRS:
        for f in sorted((ROOT / d).rglob("*.py")):
            try:
                tree = ast.parse(f.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            found += _offenders(tree, str(f.relative_to(ROOT)))
    assert not found, (
        "ошибка запроса глушится внутри транзакции — COMMIT молча откатит всё; "
        "нужна точка сохранения (вложенный conn.transaction()), ON CONFLICT или "
        "raise:\n  " + "\n  ".join(found))


def test_the_ratchet_sees_the_bug_it_guards():
    bad = ast.parse(
        "async def f(pool):\n"
        "    async with pool.acquire() as conn:\n"
        "        async with conn.transaction():\n"
        "            await conn.execute('A')\n"
        "            try:\n"
        "                await conn.execute('B')\n"
        "            except Exception:\n"
        "                pass\n")
    good = ast.parse(
        "async def f(pool):\n"
        "    async with pool.acquire() as conn:\n"
        "        async with conn.transaction():\n"
        "            try:\n"
        "                async with conn.transaction():\n"
        "                    await conn.execute('B')\n"
        "            except Exception:\n"
        "                pass\n")
    assert _offenders(bad, "x.py")
    assert not _offenders(good, "x.py")
