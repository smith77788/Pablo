"""«💬 В чаты» → «Выбрать группы»: список групп на живом Postgres.

Ручка `/api/miniapp/campaign/chats` читала `mc.member_count`, а колонка в
`managed_channels` называется `members_count`. Postgres отбивал запрос целиком,
`_safe_fetch` глушил ошибку и отдавал пустой список, поэтому выбор групп для
кампании был пуст у всех владельцев всегда, хотя `total` (второй запрос,
без этой колонки) показывал, что групп много. Тест падает на старом коде.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995301


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_campaign_chats_lists_groups_sorted_by_members(monkeypatch):
    import json

    import asyncpg
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from services import mini_app_api, mini_app_broadcast

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: OWNER)

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            acc = await pool.fetchval(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1, '+79990000301', 'sess', TRUE) RETURNING id", OWNER)
            for chat_id, members in ((-1001, 50), (-1002, 900)):
                await pool.execute(
                    "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, type, "
                    "members_count) VALUES($1, $2, $3, $4, 'megagroup', $5)",
                    OWNER, acc, chat_id, f"Группа {chat_id}", members)
            app = web.Application()
            mini_app_broadcast.setup_routes(app, pool)
            handler = next(r.handler for r in app.router.routes()
                           if getattr(r.resource, "canonical", "") == "/api/miniapp/campaign/chats")
            resp = await handler(make_mocked_request("GET", "/api/miniapp/campaign/chats"))
            return json.loads(resp.body)
        finally:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()

    data = _run(_go())
    assert data["total"] == 2
    assert [c["chat_id"] for c in data["chats"]] == [-1002, -1001], data
    assert data["chats"][0]["members"] == 900
