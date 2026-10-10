"""Доктор схемы: самодиагностика + аддитивное самолечение БД.

Проверяет разбор миграций (unit, без БД) и реальное лечение лага миграции
(e2e на живом Postgres — ровно сценарий cf_relay_url: колонка объявлена в
миграции, но в БД её нет, потому что файл не доехал).

E2e-часть запускается только с INFRAGRAM_TEST_DSN (как остальные *_postgres).
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from database import schema_doctor as sd


# ── Unit: разбор SQL без БД ───────────────────────────────────────────────────

def test_split_respects_strings_and_dollar_quotes():
    sql = ("INSERT INTO t VALUES ('a;b'); "
           "CREATE TABLE x (id int); "
           "CREATE FUNCTION f() RETURNS int AS $$ BEGIN RETURN 1; END; $$ LANGUAGE plpgsql;")
    stmts = sd.split_statements(sql)
    assert stmts[0] == "INSERT INTO t VALUES ('a;b')"
    assert stmts[1] == "CREATE TABLE x (id int)"
    # ; внутри тела функции ($$…$$) не должен был разорвать стейтмент
    assert any("LANGUAGE plpgsql" in s for s in stmts)
    assert sum("CREATE FUNCTION" in s for s in stmts) == 1


def test_make_idempotent_adds_if_not_exists():
    assert "CREATE TABLE IF NOT EXISTS" in sd._make_idempotent("CREATE TABLE foo (id int)")
    assert "ADD COLUMN IF NOT EXISTS" in sd._make_idempotent(
        "ALTER TABLE foo ADD COLUMN bar int")
    assert "INDEX IF NOT EXISTS" in sd._make_idempotent(
        "CREATE UNIQUE INDEX ix ON foo(bar)")
    # уже идемпотентный не дублируется
    once = sd._make_idempotent("CREATE TABLE IF NOT EXISTS foo (id int)")
    assert once.upper().count("IF NOT EXISTS") == 1


def test_parse_expected_collects_tables_and_alter_columns():
    texts = [
        "CREATE TABLE IF NOT EXISTS demo (id bigint primary key, a text);",
        "ALTER TABLE demo ADD COLUMN b int NOT NULL DEFAULT 0;",
        "ALTER TABLE demo ADD COLUMN IF NOT EXISTS c text, ADD COLUMN d text;",
    ]
    exp = sd.parse_expected_schema(texts)
    assert "demo" in exp.tables
    assert ("demo", "b") in exp.columns
    # несколько ADD COLUMN в одном ALTER — все попадают
    assert ("demo", "c") in exp.columns and ("demo", "d") in exp.columns
    # heal-стейтмент для колонки идемпотентен
    assert "ADD COLUMN IF NOT EXISTS" in exp.columns[("demo", "b")]


def test_real_migrations_parse_without_crash():
    """На настоящих 260+ миграциях парсер не падает и видит знаковые объекты."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exp = sd.parse_expected_schema(sd._migration_texts(root))
    assert len(exp.tables) > 100, "подозрительно мало таблиц — парсер сломан?"
    assert "account_flood_log" in exp.tables
    # cf_relay_url — та самая колонка, потерянная при коллизии basename v153
    assert ("tg_accounts", "cf_relay_url") in exp.columns


# ── E2e: лечение на живом Postgres ────────────────────────────────────────────

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
e2e = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@e2e
def test_heal_fixes_migration_lag():
    import asyncpg

    async def scenario():
        tmp = tempfile.mkdtemp()
        # Колонка b объявлена ПОЗДНЕЙ миграцией, таблица later — тоже отдельной.
        with open(os.path.join(tmp, "schema_v1.sql"), "w") as f:
            f.write("CREATE TABLE IF NOT EXISTS sd_demo (id BIGINT PRIMARY KEY, a TEXT);")
        with open(os.path.join(tmp, "schema_v2.sql"), "w") as f:
            f.write("ALTER TABLE sd_demo ADD COLUMN b INT NOT NULL DEFAULT 0;")
        with open(os.path.join(tmp, "schema_v3.sql"), "w") as f:
            f.write("CREATE TABLE sd_later (id BIGINT PRIMARY KEY, note TEXT);")

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            # Симулируем лаг: применён только v1 (sd_demo без b), sd_later нет.
            await pool.execute("DROP TABLE IF EXISTS sd_demo, sd_later")
            await pool.execute("CREATE TABLE sd_demo (id BIGINT PRIMARY KEY, a TEXT)")

            miss_t, miss_c, _ = await sd.diagnose(pool, root=tmp)
            assert "sd_later" in miss_t, "не увидел недостающую таблицу"
            assert ("sd_demo", "b") in miss_c, "не увидел недостающую колонку"

            rep = await sd.heal(pool, root=tmp)
            assert "sd_later" in rep.created_tables
            assert "sd_demo.b" in rep.added_columns
            assert not rep.errors, f"heal с ошибками: {rep.errors}"

            # Факт в БД: колонка и таблица теперь есть.
            cols = {r["column_name"] for r in await pool.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='sd_demo'")}
            assert "b" in cols
            assert await pool.fetchval("SELECT to_regclass('public.sd_later')") is not None

            # Идемпотентность: повторный heal ничего не меняет.
            rep2 = await sd.heal(pool, root=tmp)
            assert not rep2.healed_anything, "повторный heal не идемпотентен"
        finally:
            await pool.execute("DROP TABLE IF EXISTS sd_demo, sd_later")
            await pool.close()

    _run(scenario())
