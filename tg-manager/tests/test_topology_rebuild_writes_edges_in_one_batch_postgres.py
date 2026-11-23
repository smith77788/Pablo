"""«Обновить граф» пишет рёбра одной пачкой, теми же строками, что раньше.

Кнопка делала INSERT в behavioral_events на каждый канал сети: тысячи каналов
— тысячи запросов и десятки секунд ожидания в боте.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 995911


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_handler_uses_the_batch():
    from bot.handlers import topology
    src = inspect.getsource(topology.cb_topo_rebuild)
    assert "record_cross_nav_many(" in src
    assert "record_cross_nav(" not in src.replace("record_cross_nav_many(", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
def test_batch_rows_match_single_rows():
    import asyncpg

    from services import behavioral_engine as be

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM behavioral_events WHERE owner_id=$1", OWNER)
            await be.record_cross_nav(pool, OWNER, "account", 1, "channel", 100)
            n = await be.record_cross_nav_many(
                pool, OWNER, [("account", 1, "channel", 100), ("bot", 7, "account", 1)])
            rows = await pool.fetch(
                "SELECT entity_type, entity_id, event_type, meta FROM behavioral_events "
                "WHERE owner_id=$1 ORDER BY id", OWNER)
            return n, [(r["entity_type"], r["entity_id"], r["event_type"],
                        json.loads(r["meta"])) for r in rows]
        finally:
            await pool.execute("DELETE FROM behavioral_events WHERE owner_id=$1", OWNER)
            await pool.close()

    n, rows = _run(_go())
    assert n == 2
    assert rows[0] == rows[1], "пачка пишет то же, что поштучная запись"
    assert rows[2][:3] == ("bot", 7, "cross_nav") and rows[2][3]["to_type"] == "account"
