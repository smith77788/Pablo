"""Воронка бота: копия со всеми шагами в другого бота.

Одинаковую цепочку на нескольких ботах собирали руками. Копия обязана
прийти ВЫКЛЮЧЕННОЙ (на другом боте другая аудитория), сохранить порядок,
тексты и задержки шагов и не пускать ни к чужой воронке, ни в чужого бота.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990841, 990842
BOT_A, BOT_B, BOT_X = 9908411, 9908412, 9908421


def test_detail_offers_copy():
    ui = miniapp_source()
    assert 'onclick="copyFunnelToBot()"' in ui
    assert "'/api/miniapp/funnel/' + Number(CUR_FUNNEL_ID) + '/copy'" in ui


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_copy_carries_steps_starts_disabled_and_is_scoped():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_funnels as F

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        bots = [BOT_A, BOT_B, BOT_X]

        async def clean():
            await pool.execute("DELETE FROM funnel_steps WHERE funnel_id IN "
                               "(SELECT id FROM funnels WHERE bot_id = ANY($1::bigint[]))", bots)
            await pool.execute("DELETE FROM funnels WHERE bot_id = ANY($1::bigint[])", bots)
            await pool.execute("DELETE FROM managed_bots WHERE bot_id = ANY($1::bigint[])", bots)
        try:
            await clean()
            for b, who in ((BOT_A, OWNER), (BOT_B, OWNER), (BOT_X, STRANGER)):
                await pool.execute("INSERT INTO managed_bots(token, bot_id, added_by) VALUES($1,$2,$3)",
                                   f"t{b}", b, who)
            src = await pool.fetchval(
                "INSERT INTO funnels(bot_id, name, trigger_type, keyword, is_active)"
                " VALUES($1, 'Прогрев', 'keyword', 'старт', TRUE) RETURNING id", BOT_A)
            await pool.executemany(
                "INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes)"
                " VALUES($1,$2,$3,$4)", [(src, 1, "Привет", 0), (src, 2, "Ещё", 1440)])
            alien = await pool.fetchval(
                "INSERT INTO funnels(bot_id, name, trigger_type) VALUES($1, 'Чужая', 'start') RETURNING id",
                BOT_X)
            app = web.Application()
            F.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.post(f"/api/miniapp/funnel/{src}/copy", json={"bot_id": BOT_B})
                assert r.status == 200, await r.text()
                d = await r.json()
                assert d["steps"] == 2
                f = await pool.fetchrow("SELECT * FROM funnels WHERE id=$1", d["funnel_id"])
                assert f["bot_id"] == BOT_B and f["is_active"] is False
                assert (f["trigger_type"], f["keyword"]) == ("keyword", "старт")
                steps = [tuple(x) for x in await pool.fetch(
                    "SELECT step_order, message_text, delay_minutes FROM funnel_steps"
                    " WHERE funnel_id=$1 ORDER BY step_order", d["funnel_id"])]
                assert steps == [(1, "Привет", 0), (2, "Ещё", 1440)]
                # Чужой бот как цель и чужая воронка как источник — отказ.
                assert (await client.post(f"/api/miniapp/funnel/{src}/copy",
                                          json={"bot_id": BOT_X})).status == 404
                assert (await client.post(f"/api/miniapp/funnel/{alien}/copy",
                                          json={"bot_id": BOT_B})).status == 404
                assert await pool.fetchval("SELECT count(*) FROM funnels WHERE bot_id=$1", BOT_X) == 1
                assert (await client.post(f"/api/miniapp/funnel/{src}/copy", json={})).status == 400
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await clean()
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
