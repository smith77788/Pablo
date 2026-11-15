"""Возврат соединения в пул не стоит лишнего похода в базу — и не теряет сброс.

asyncpg на каждом возврате соединения выполнял
`pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;` и ждал ответа.
Любой `pool.fetchval` был двумя походами в базу: при 20 мс до базы — 43 мс
вместо 21. /start (11 обращений) и главная мини-аппа (десятки) платили это
на каждом открытии, а соединение всё это время было занято для остальных.

`database/lean_reset.LeanResetConnection` сбрасывает сессию только тому
соединению, через которое прошло что-то, оставляющее состояние. Здесь
проверено на живой базе обе стороны:
  * чистое соединение возвращается без запроса сброса;
  * `SET`, сессионная advisory-блокировка и `set_config` по-прежнему
    сбрасываются — следующий взявший соединение их не видит;
  * открытая транзакция на возврате откатывается, как и раньше.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from database.lean_reset import LeanResetConnection, leaves_session_state

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.parametrize("sql", [
    "SET lock_timeout = '5s'",
    "set statement_timeout to 100",
    "SELECT 1; SET search_path TO x",
    "-- коммент\nSET session_replication_role = replica",
    "/* c */ SET role x",
    "RESET ALL",
    "LISTEN chan",
    "DECLARE c CURSOR WITH HOLD FOR SELECT 1",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_try_advisory_lock(1, 2)",
    "SELECT pg_advisory_lock_shared(5)",
    "SELECT set_config('a.b', 'c', false)",
])
def test_session_state_is_recognised(sql):
    assert leaves_session_state(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "UPDATE t SET a = 1 WHERE id = $1",
    "UPDATE t\nSET a = 1",
    "INSERT INTO t(a) VALUES ($1) ON CONFLICT (a) DO UPDATE SET a = EXCLUDED.a",
    "SET LOCAL lock_timeout = '1s'",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_xact_lock(7)",
    "SELECT * FROM t WHERE reset_at > now()",
])
def test_plain_queries_are_clean(sql):
    assert not leaves_session_state(sql)


def test_create_pool_uses_the_lean_connection():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "database", "db.py"), encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("async def create_pool("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "connection_class=LeanResetConnection" in body


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_reset_only_when_needed_on_a_live_base():
    import asyncpg

    async def go():
        pool = await asyncpg.create_pool(
            DSN, min_size=1, max_size=1, connection_class=LeanResetConnection)
        resets: list[str] = []
        try:
            async with pool.acquire() as c:
                raw = c._con if hasattr(c, "_con") else c
                orig = type(raw)._get_reset_query

                def spy(self):
                    q = orig(self)
                    resets.append(q)
                    return q
                type(raw)._get_reset_query = spy
            try:
                # Обычные запросы — без сброса.
                resets.clear()
                for _ in range(3):
                    assert await pool.fetchval("SELECT 1") == 1
                assert resets and all(q == "" for q in resets), resets

                # SET без LOCAL: следующий взявший соединение не должен его видеть.
                default = await pool.fetchval("SHOW statement_timeout")
                async with pool.acquire() as c:
                    await c.execute("SET statement_timeout = '4321ms'")
                assert await pool.fetchval("SHOW statement_timeout") == default

                # set_config(..., false) — тоже состояние сессии.
                await pool.fetchval("SELECT set_config('application_name', 'leak', false)")
                assert await pool.fetchval("SHOW application_name") != "leak"

                # Сессионная advisory-блокировка снимается на возврате.
                await pool.fetchval("SELECT pg_advisory_lock(424242)")
                other = await asyncpg.connect(DSN)
                try:
                    assert await other.fetchval("SELECT pg_try_advisory_lock(424242)")
                    await other.fetchval("SELECT pg_advisory_unlock(424242)")
                finally:
                    await other.close()

                # Брошенная транзакция откатывается, как и раньше.
                await pool.execute("CREATE TABLE IF NOT EXISTS lean_reset_probe(x int)")
                await pool.execute("TRUNCATE lean_reset_probe")
                c = await pool.acquire()
                tr = c.transaction()
                await tr.start()
                await c.execute("INSERT INTO lean_reset_probe VALUES (1)")
                await pool.release(c)
                assert await pool.fetchval("SELECT count(*) FROM lean_reset_probe") == 0
                await pool.execute("DROP TABLE lean_reset_probe")
            finally:
                type(raw)._get_reset_query = orig
        finally:
            await pool.close()

    asyncio.run(go())
