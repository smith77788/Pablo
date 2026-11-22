"""Правила редактора канала: вернуться к общим и взять общие за основу.

Свои правила канала можно было завести, но не снять: канал навсегда
отрывался от общих правил, и их правка его больше не касалась. А новые
правила канала начинались с пустой формы, хотя общие уже были заданы.
Запрос удаления новый — проверка на живой базе, вместе с владельцем.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990881, 990882
CH = -1009900881
JS = (Path(__file__).resolve().parent.parent / "mini_app" / "screens" / "editorial.js").read_text(encoding="utf-8")


def test_channel_screen_offers_both_actions():
    assert "onclick=\"edResetToGeneral()\"" in JS
    assert "onclick=\"edCopyGeneral()\"" in JS
    assert "api(_edPath(), {method: 'DELETE'})" in JS


@pytest.mark.skipif(not DSN, reason="нужна INFRAGRAM_TEST_DSN")
def test_channel_rules_can_be_dropped_general_cannot():
    async def go():
        import asyncpg
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from services import mini_app_api as M

        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        orig = M._get_uid
        M._get_uid = lambda r: OWNER
        owners = [OWNER, STRANGER]
        try:
            await pool.execute("DELETE FROM va_channel_brain WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title) VALUES($1, 0, $2, 'Мой')",
                OWNER, CH)
            ins = ("INSERT INTO va_channel_brain(owner_id, channel_key, brand_rules) "
                   "VALUES($1, $2, $3::jsonb)")
            rules = json.dumps({"forbidden_words": ["скидка"]})
            await pool.execute(ins, OWNER, "*", rules)
            await pool.execute(ins, OWNER, str(CH), rules)
            await pool.execute(ins, STRANGER, str(CH), rules)
            app = web.Application()
            M.setup_routes(app, pool)
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                keys = "SELECT channel_key FROM va_channel_brain WHERE owner_id=$1 ORDER BY 1"
                # Общие правила так не удалить.
                r = await client.delete("/api/miniapp/editorial/policy")
                assert r.status == 400
                # Свои правила канала снимаются; общие и чужие остаются.
                r = await client.delete(f"/api/miniapp/editorial/policy?channel={CH}")
                assert r.status == 200, await r.text()
                assert (await r.json())["removed"] is True
                assert [x["channel_key"] for x in await pool.fetch(keys, OWNER)] == ["*"]
                assert [x["channel_key"] for x in await pool.fetch(keys, STRANGER)] == [str(CH)]
                # Чужой канал — 404.
                r = await client.delete("/api/miniapp/editorial/policy?channel=-1001")
                assert r.status == 404
                # Повтор — не ошибка, просто нечего снимать.
                r = await client.delete(f"/api/miniapp/editorial/policy?channel={CH}")
                assert r.status == 200 and (await r.json())["removed"] is False
            finally:
                await client.close()
        finally:
            M._get_uid = orig
            await pool.execute("DELETE FROM va_channel_brain WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.execute("DELETE FROM managed_channels WHERE owner_id = ANY($1::bigint[])", owners)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    finally:
        loop.close()
