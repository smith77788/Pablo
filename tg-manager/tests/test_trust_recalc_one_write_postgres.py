"""Пересчёт доверия пишет весь флот одним запросом.

`_recalculate_scores` делал UPDATE на каждый активный аккаунт ВСЕХ владельцев
каждые 30 минут: на стенде с 2500 аккаунтами — 2500 запросов и ~0,5 с;
теперь один UPDATE ... FROM unnest и ~30–70 мс. Неизменившееся число не
перезаписывается: trust_score в индексе, «пустая» запись — новая версия строки.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
OWNER = 995411


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_scores_written_in_one_query_and_correct():
    import asyncpg

    from services import trust_engine as te

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            for i, floods in enumerate((0, 2, 5)):
                await pool.execute(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active, "
                    "flood_count_7d, trust_score, added_at) "
                    "VALUES($1,$2,'s',TRUE,$3,0.5,NOW())",
                    OWNER, f"+7990000{i}", floods)

            writes = []

            class _P:
                async def fetch(self, *a):
                    return await pool.fetch(*a)

                async def execute(self, q, *a):
                    if "trust_score" in q and "UPDATE" in q:
                        writes.append(q)
                    return await pool.execute(q, *a)

                async def executemany(self, *a):
                    return await pool.executemany(*a)

            await te._recalculate_scores(_P())
            got = [r["trust_score"] for r in await pool.fetch(
                "SELECT trust_score FROM tg_accounts WHERE owner_id=$1 ORDER BY flood_count_7d",
                OWNER)]
            return writes, got
        finally:
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()

    writes, got = _run(_go())
    per_account = [q for q in writes if "unnest" not in q and "acc_status IN" not in q]
    assert not per_account, f"запрос на аккаунт: {per_account[:1]}"
    assert got[0] == pytest.approx(1.0) and got[0] > got[1] > got[2], got
