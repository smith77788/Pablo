"""Массовое назначение пула — один запрос и только аккаунтам без пула.

НАХОДКА (09.11.2026, прогон бота на 10 000 аккаунтов). Кнопка «Назначить пул
аккаунтам» шла по два запроса на аккаунт (прочитать, записать): 20 000
обращений к базе подряд в одном нажатии. Теперь одно UPDATE, и правило то же:
пул получают только аккаунты без пула, чужие аккаунты не трогаются.
"""
from __future__ import annotations

import asyncio
import os
import random

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

OWNER = 980_000_000 + random.randint(1, 999_999)


def test_assigns_only_unpooled_and_only_own():
    from database import db

    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            ids = []
            for i, p in enumerate([None, "", "main", None]):
                ids.append(await pool.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, pool)"
                    " VALUES($1,$2,'s',TRUE,$3) RETURNING id", OWNER, f"+7998{OWNER % 100000}{i:03d}", p))
            foreign = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active)"
                " VALUES($1,$2,'s',TRUE) RETURNING id", OWNER + 1, f"+7997{OWNER % 100000}000")
            n = await db.assign_pool_to_unpooled(pool, OWNER, ids + [foreign], "strike")
            rows = dict(await pool.fetch(
                "SELECT id, pool FROM tg_accounts WHERE id = ANY($1::int[])", ids + [foreign]))
            assert n == 3
            assert [rows[i] for i in ids] == ["strike", "strike", "main", "strike"]
            assert rows[foreign] is None
            assert await db.assign_pool_to_unpooled(pool, OWNER, [], "x") == 0
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, OWNER + 1])
            await pool.close()

    asyncio.run(go())
