"""Призрак: на все аккаунты одним нажатием и общий выключатель.

Профиль заводился по одному аккаунту, а выключить присутствие всего флота
можно было только строка за строкой. Запросы новые — проверка на настоящей
базе: создаются профили ровно для рабочих аккаунтов владельца без профиля,
настроенные профили не перезаписываются, чужие не задеваются.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_html, miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990811, 990812


def test_screen_offers_bulk_controls():
    assert 'id="gc-all"' in miniapp_html() and 'id="ghostBulk"' in miniapp_html()
    ui = miniapp_source()
    assert "'/api/miniapp/ghost/bulk'" in ui
    assert "'/api/miniapp/ghost/bulk_toggle'" in ui
    assert "saveFormPrefs('ghost'" in ui and "applyFormPrefs('ghost'" in ui


def _run(body):
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_presence as P

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER

        async def clean():
            await pool.execute("DELETE FROM ghost_profiles WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
        try:
            await clean()
            accs = {}
            for key, who, active in (("a", OWNER, True), ("b", OWNER, True),
                                     ("dead", OWNER, False), ("tuned", OWNER, True),
                                     ("alien", STRANGER, True)):
                accs[key] = await pool.fetchval(
                    "INSERT INTO tg_accounts(owner_id, phone, session_str, first_name, is_active)"
                    " VALUES($1, $2, 'x', $3, $4) RETURNING id",
                    who, f"+7998{len(accs)}", key, active)
            await pool.execute(
                "INSERT INTO ghost_profiles(owner_id, account_id, personality, daily_cap, enabled)"
                " VALUES($1, $2, 'active', 3, FALSE)", OWNER, accs["tuned"])
            await pool.execute(
                "INSERT INTO ghost_profiles(owner_id, account_id, enabled) VALUES($1, $2, FALSE)",
                STRANGER, accs["alien"])
            app = web.Application()
            P.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                return await body(client, pool, accs)
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            try:
                await clean()
            finally:
                await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


pg = pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")


@pg
def test_bulk_create_covers_only_working_accounts_without_profile():
    async def body(client, pool, accs):
        r = await client.post("/api/miniapp/ghost/bulk", json={
            "personality": "watcher", "active_hours_start": 10, "active_hours_end": 20,
            "daily_cap": 5})
        assert r.status == 200, await r.text()
        assert (await r.json())["created"] == 2
        rows = {x["account_id"]: dict(x) for x in await pool.fetch(
            "SELECT account_id, personality, daily_cap FROM ghost_profiles WHERE owner_id=$1",
            OWNER)}
        assert set(rows) == {accs["a"], accs["b"], accs["tuned"]}
        assert rows[accs["a"]]["personality"] == "watcher" and rows[accs["a"]]["daily_cap"] == 5
        # Настроенный профиль не перезаписан.
        assert rows[accs["tuned"]]["personality"] == "active" and rows[accs["tuned"]]["daily_cap"] == 3
        again = await client.post("/api/miniapp/ghost/bulk", json={"personality": "ghost"})
        assert (await again.json())["created"] == 0
    _run(body)


@pg
def test_bulk_create_validates_fields():
    async def body(client, pool, accs):
        r = await client.post("/api/miniapp/ghost/bulk", json={"active_hours_start": 99})
        assert r.status == 400
        assert await pool.fetchval(
            "SELECT count(*) FROM ghost_profiles WHERE owner_id=$1", OWNER) == 1
    _run(body)


@pg
def test_bulk_toggle_touches_only_own_profiles():
    async def body(client, pool, accs):
        await client.post("/api/miniapp/ghost/bulk", json={})
        r = await client.put("/api/miniapp/ghost/bulk_toggle", json={"enabled": False})
        assert (await r.json())["changed"] == 2
        assert await pool.fetchval(
            "SELECT count(*) FROM ghost_profiles WHERE owner_id=$1 AND enabled", OWNER) == 0
        r = await client.put("/api/miniapp/ghost/bulk_toggle", json={"enabled": True})
        assert (await r.json())["changed"] == 3
        assert await pool.fetchval(
            "SELECT enabled FROM ghost_profiles WHERE owner_id=$1", STRANGER) is False
        bad = await client.put("/api/miniapp/ghost/bulk_toggle", json={"enabled": "yes"})
        assert bad.status == 400
    _run(body)
