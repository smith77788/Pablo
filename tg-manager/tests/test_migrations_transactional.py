"""Миграции применяются ЦЕЛИКОМ или НИКАК, и критичный сбой роняет старт.

Находка аудита №4. Раньше операторы файла шли по одному в автокоммите: сбой на
пятом из десяти оставлял первые четыре применёнными, остальные всё равно
выполнялись — схема застывала полуприменённой, а процесс спокойно стартовал на
ней. Отсюда же защитные до-миграции колонок прямо в main.py: дрейф схемы считался
нормой.

Заглушкой это не проверяется в принципе: нужен настоящий Postgres, где видно,
что откат действительно откатывает. Запуск — как в tests/test_account_lease_postgres
(INFRAGRAM_TEST_DSN); без переменной файл пропускается.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await p.execute("CREATE SCHEMA IF NOT EXISTS migtest")
        return p

    p = _run(_mk())
    yield p
    _run(p.close())


async def _apply(conn, statements):
    """Зовём БОЕВУЮ функцию применения файла, а не копию её логики.

    Копия проверяла бы сама себя: подмени db.py на непонтранзакционный вариант —
    поведенческие тесты остались бы зелёными.
    """
    from database.db import apply_migration_statements
    ok, _errs, _last = await apply_migration_statements(conn, statements, "test")
    return ok


def test_failed_file_rolls_back_everything(pool):
    """Сбой на середине файла НЕ должен оставлять первые операторы применёнными."""
    async def go():
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS migtest.a, migtest.b CASCADE")
            ok = await _apply(conn, [
                "CREATE TABLE migtest.a(id int)",          # применится…
                "CREATE TABLE migtest.b(id int)",          # …и это…
                "CREATE TABLE migtest.c(id int) BAD SYNTAX",   # …а тут сбой
            ])
            assert ok is False
            for t in ("a", "b"):
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='migtest' AND table_name=$1)", t)
                assert exists is False, (
                    f"таблица {t} осталась после сбоя файла — схема полуприменена")
    _run(go())


def test_successful_file_is_committed(pool):
    async def go():
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS migtest.ok1 CASCADE")
            ok = await _apply(conn, [
                "CREATE TABLE migtest.ok1(id int)",
                "CREATE INDEX idx_migtest_ok1 ON migtest.ok1(id)",
            ])
            assert ok is True
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='migtest' AND table_name='ok1')")
            assert exists is True
    _run(go())


def test_already_exists_does_not_abort_the_rest(pool):
    """Идемпотентный повтор обязан работать: «already exists» — не сбой.

    Именно ради этого savepoint на каждый оператор. Без него первый же
    повторный CREATE переводил бы транзакцию в aborted и терял хвост файла.
    """
    async def go():
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS migtest.dup, migtest.tail CASCADE")
            await conn.execute("CREATE TABLE migtest.dup(id int)")   # уже есть
            ok = await _apply(conn, [
                "CREATE TABLE migtest.dup(id int)",      # already exists → пропускаем
                "CREATE TABLE migtest.tail(id int)",     # ОБЯЗАН примениться
            ])
            assert ok is True
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='migtest' AND table_name='tail')")
            assert exists is True, "хвост файла потерян после «already exists»"
    _run(go())


def test_critical_tables_missing_raises():
    """Отсутствие критичных таблиц обязано ронять старт, а не логироваться."""
    from database import db
    assert hasattr(db, "SchemaMigrationError")
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "database", "db.py"), encoding="utf-8").read()
    i = src.index("_CRITICAL_TABLES")
    tail = src[i:]
    assert "raise SchemaMigrationError" in tail, (
        "критичные таблицы снова только логируются — процесс стартует на нерабочей схеме")
