"""Экосистема: переименование и описание без пересоздания.

Править экосистему было нельзя: опечатка в названии лечилась только
удалением, а удаление снимает участников и доступы. Запрос новый —
проверка на живой базе, вместе с владельцем.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990831, 990832


def test_detail_offers_edit():
    ui = miniapp_source()
    assert 'onclick="ecoEdit()"' in ui
    assert "'/api/miniapp/ecosystem/' + Number(CUR_ECO_ID), {method:'PUT'" in ui


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_rename_is_owner_scoped_and_validated():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M
        from services import mini_app_networks as N

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        try:
            await pool.execute("DELETE FROM ecosystems WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            mine = await pool.fetchval(
                "INSERT INTO ecosystems(owner_id, name) VALUES($1, 'Опечтака') RETURNING id", OWNER)
            alien = await pool.fetchval(
                "INSERT INTO ecosystems(owner_id, name) VALUES($1, 'Чужая') RETURNING id", STRANGER)
            app = web.Application()
            N.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                r = await client.put(f"/api/miniapp/ecosystem/{mine}",
                                     json={"name": " Опечатка ", "description": "Боты региона"})
                assert r.status == 200, await r.text()
                row = await pool.fetchrow("SELECT name, description FROM ecosystems WHERE id=$1", mine)
                assert (row["name"], row["description"]) == ("Опечатка", "Боты региона")
                r = await client.put(f"/api/miniapp/ecosystem/{alien}", json={"name": "Моя"})
                assert r.status == 404
                assert await pool.fetchval("SELECT name FROM ecosystems WHERE id=$1", alien) == "Чужая"
                r = await client.put(f"/api/miniapp/ecosystem/{mine}", json={"name": "  "})
                assert r.status == 400
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM ecosystems WHERE owner_id = ANY($1::bigint[])",
                               [OWNER, STRANGER])
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
