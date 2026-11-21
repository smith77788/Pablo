"""Фабрика каналов: созданный канал открывается из списка «Недавние».

Строка была просто текстом, а ручка не отдавала channel_id — открыть
только что созданный канал можно было лишь через общий список каналов.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 990821


def test_row_opens_the_channel():
    assert 'onclick="openChannel(${Number(r.channel_id)})"' in miniapp_source()


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_recent_returns_channel_id():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        try:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title) VALUES($1, 0, $2, 'Новый')",
                OWNER, -1009900821)
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.get("/api/miniapp/channel_factory/recent")
                assert r.status == 200, await r.text()
                items = (await r.json())["items"]
                assert [int(x["channel_id"]) for x in items] == [-1009900821]
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()


def test_bot_factory_recent_row_opens_the_bot():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "bot_factory.js").read_text(
        encoding="utf-8")
    assert 'onclick="openBot(${Number(b.bot_id)})"' in js
