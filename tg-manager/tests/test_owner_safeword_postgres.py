"""Секретное слово на живой базе (schema_v382): задать, сменить, снять,
подсказка, и хвост line() для готовой тревоги.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import owner_safeword as sw

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 776221
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")


def test_set_change_clear_and_line():
    async def _go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM owner_safeword WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

            # Нет слова — хвост пустой, подсказки нет (нет аккаунтов).
            assert await sw.get(pool, OWNER) is None
            assert await sw.line(pool, OWNER) == ""
            assert await sw.suggested(pool, OWNER) is False

            # Есть аккаунт, слова нет → подсказка.
            await pool.execute(
                "INSERT INTO tg_accounts(owner_id, phone, session_str) VALUES($1,'+1','s')", OWNER)
            assert await sw.suggested(pool, OWNER) is True

            # Задать.
            assert (await sw.set_phrase(pool, OWNER, "  синий кит "))["ok"]
            assert await sw.get(pool, OWNER) == "синий кит"
            assert "синий кит" in await sw.line(pool, OWNER)
            assert await sw.suggested(pool, OWNER) is False, "слово есть — подсказки нет"

            # Слишком короткое — отказ, прежнее не тронуто.
            assert not (await sw.set_phrase(pool, OWNER, "x"))["ok"]
            assert await sw.get(pool, OWNER) == "синий кит"

            # Сменить.
            assert (await sw.set_phrase(pool, OWNER, "красный слон"))["ok"]
            assert await sw.get(pool, OWNER) == "красный слон"

            # Снять.
            assert (await sw.clear(pool, OWNER))["ok"]
            assert await sw.get(pool, OWNER) is None
            assert await sw.line(pool, OWNER) == ""
        finally:
            await pool.execute("DELETE FROM owner_safeword WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()
    asyncio.new_event_loop().run_until_complete(_go())
