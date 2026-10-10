"""Старт веб-процесса не берёт 30 блокировок таблиц заново каждый раз.

Инлайн-миграции гнались целиком при каждом старте: 75 отдельных `pool.execute`,
из них 30 `ALTER TABLE`. `ADD COLUMN IF NOT EXISTS` берёт `ACCESS EXCLUSIVE`
даже когда делать нечего, а потолка ожидания блокировки на этом пути не было —
`SET lock_timeout` в `create_pool` действует на одно соединение, которое он сам
взял, а не на пул. Обычный деплой мог встать насмерть: новый контейнер ждёт
блокировку старого, за ожиданием выстраиваются все читатели, снаружи —
«приложение не загружается».
"""
from __future__ import annotations

import asyncio

import pytest

from services import mini_app_api as M


class _Conn:
    def __init__(self, journal=(), fail_journal=False, fail_on=()):
        self.journal = set(journal)
        self.fail_journal = fail_journal
        self.fail_on = tuple(fail_on)
        self.executed: list[str] = []
        self.fetched: list[str] = []

    async def execute(self, q, *a):
        one = " ".join(q.split())
        self.executed.append(one)
        if "INSERT INTO schema_migrations" in one:
            if self.fail_journal:
                raise RuntimeError("журнал недоступен")
            self.journal.add(a[0])
            return "INSERT 1"
        for bad in self.fail_on:
            if bad in one:
                raise RuntimeError(f"не получилось: {bad}")
        return "OK"

    async def fetch(self, q, *a):
        one = " ".join(q.split())
        self.fetched.append(one)
        if "FROM schema_migrations" in one:
            if self.fail_journal:
                raise RuntimeError("журнала ещё нет")
            return [{"filename": k} for k in a[0] if k in self.journal]
        return []


class _Pool:
    """Пул, который умеет отдать РОВНО ОДНО соединение и посчитать заходы."""

    def __init__(self, conn):
        self.conn = conn
        self.acquires = 0

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                pool.acquires += 1
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def execute(self, q, *a):      # pragma: no cover — сюда ходить нельзя
        raise AssertionError(
            "накат ходит через pool.execute: каждый оператор берёт своё "
            "соединение, а потолок ожидания блокировки теряется")


STMTS = [
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS a TEXT",
    "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS b TEXT",
]


def _run(pool, stmts=STMTS):
    return asyncio.run(M.apply_inline_migrations(pool, stmts))


def test_lock_timeout_is_set_on_the_same_connection():
    """Без потолка на ЭТОМ соединении DDL ждёт блокировку бесконечно."""
    conn = _Conn()
    pool = _Pool(conn)
    _run(pool)
    assert pool.acquires == 1, "соединение берётся не один раз на весь накат"
    assert "lock_timeout" in conn.executed[0], (
        f"первым делом ставится потолок ожидания блокировки, а не {conn.executed[0]!r}")


def test_first_start_applies_everything_and_records_it():
    conn = _Conn()
    assert _run(_Pool(conn)) == {"applied": 2, "skipped": 0, "failed": 0}
    assert len(conn.journal) == 2, "применённое не попало в журнал"


def test_second_start_takes_no_locks_at_all():
    """Главное: обычный деплой не трогает ни одной таблицы."""
    conn = _Conn()
    _run(_Pool(conn))
    second = _Conn(journal=conn.journal)
    assert _run(_Pool(second)) == {"applied": 0, "skipped": 2, "failed": 0}
    assert not [q for q in second.executed if "ALTER TABLE" in q], (
        "на повторном старте всё равно берутся блокировки таблиц")


def test_changed_statement_is_applied_again():
    """Ключ — отпечаток текста: правка по смыслу обязана доехать до базы."""
    conn = _Conn()
    _run(_Pool(conn))
    changed = STMTS[:1] + ["ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS c TEXT"]
    second = _Conn(journal=conn.journal)
    res = asyncio.run(M.apply_inline_migrations(_Pool(second), changed))
    assert res == {"applied": 1, "skipped": 1, "failed": 0}
    assert any("COLUMN IF NOT EXISTS c" in q for q in second.executed)


def test_reformatting_alone_does_not_replay():
    """Отступы и переводы строк в списке — не смысловая правка."""
    conn = _Conn()
    _run(_Pool(conn), ["CREATE TABLE IF NOT EXISTS t (id BIGINT)"])
    second = _Conn(journal=conn.journal)
    res = asyncio.run(M.apply_inline_migrations(
        _Pool(second), ["CREATE  TABLE IF NOT EXISTS\n    t (id BIGINT)"]))
    assert res["skipped"] == 1, "переформатирование погнало миграцию заново"


def test_failed_statement_is_not_recorded_and_retries_next_start():
    """Прежний self-heal: не применилось — доигрывается на следующем запуске."""
    conn = _Conn(fail_on=("COLUMN IF NOT EXISTS b",))
    assert _run(_Pool(conn)) == {"applied": 1, "skipped": 0, "failed": 1}
    second = _Conn(journal=conn.journal)
    assert _run(_Pool(second))["applied"] == 1, "неудавшийся оператор не повторился"


def test_unreadable_journal_replays_everything():
    """Журнала нет (первый старт, старая база) — ведём себя как раньше."""
    conn = _Conn(fail_journal=True)
    assert _run(_Pool(conn)) == {"applied": 2, "skipped": 0, "failed": 0}


def test_lock_timeout_error_is_not_confused_with_a_broken_migration():
    conn = _Conn()

    async def _boom(q, *a):
        one = " ".join(q.split())
        conn.executed.append(one)
        if "ALTER TABLE" in one:
            raise RuntimeError("canceling statement due to lock timeout")
        return "OK"

    conn.execute = _boom
    res = asyncio.run(M.apply_inline_migrations(_Pool(conn), STMTS))
    assert res == {"applied": 0, "skipped": 0, "failed": 2}


def test_replay_env_ignores_the_journal(monkeypatch):
    """Ручное лечение схемы: колонку снесли — надо прогнать всё заново."""
    conn = _Conn()
    _run(_Pool(conn))
    monkeypatch.setenv("INFRAGRAM_INLINE_REPLAY", "1")
    second = _Conn(journal=conn.journal)
    assert _run(_Pool(second))["applied"] == 2
    assert not second.fetched, "с INFRAGRAM_INLINE_REPLAY журнал читать незачем"


def test_startup_hook_uses_the_shared_applier():
    """Обработчик on_startup не должен снова обрастать своим циклом."""
    import ast

    src = open(M.__file__, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_apply_inline_migrations"):
            body = "\n".join(src.split("\n")[node.lineno - 1:node.end_lineno])
            break
    else:
        raise AssertionError("обработчик _apply_inline_migrations не найден")
    assert "apply_inline_migrations(pool)" in body
    assert "pool.execute" not in body, (
        "накат снова ходит через pool.execute — потолок ожидания блокировки "
        "перестал действовать")


def test_lock_timeout_helper_is_the_single_source_of_truth():
    """Потолок разбирается в одном месте, а не переписывается по путям."""
    from database import db

    src = open(db.__file__, encoding="utf-8").read()
    assert src.count("SET lock_timeout") == 1, (
        "SET lock_timeout снова выписан в нескольких местах — значения разойдутся")
    assert db.schema_lock_timeout() == "5s"


def test_broken_env_value_falls_back_to_default(monkeypatch):
    from database import db

    monkeypatch.setenv("SCHEMA_LOCK_TIMEOUT", "навсегда")
    assert db.schema_lock_timeout() == "5s"
    monkeypatch.setenv("SCHEMA_LOCK_TIMEOUT", "250ms")
    assert db.schema_lock_timeout() == "250ms"
