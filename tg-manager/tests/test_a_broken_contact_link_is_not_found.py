"""Испорченная ссылка на контакт — «не найден» (404), а не сбой сервера.

Обход мини-аппа 08.11.2026: карточка контакта по номеру «1» (старая или
испорченная ссылка) отдавала «Внутренняя ошибка сервера» (500) на пяти
запросах — asyncpg падал на приведении «1» к UUID. Номер проверяется до
похода в базу; пул здесь нарочно падает на любом запросе.
"""
from __future__ import annotations

import asyncio


class _BoomPool:
    def __getattr__(self, name):
        async def boom(*a, **k):
            raise AssertionError("до базы дело дойти не должно")
        return boom


def test_non_uuid_contact_id_is_404_on_every_card_route(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from services import mini_app_api, mini_app_uch
    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 7)
    app = web.Application()
    mini_app_uch.setup_routes(app, _BoomPool())

    async def go():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            out = {}
            for tail in ("", "/crm", "/versions", "/relationships", "/timeline", "/identity"):
                r = await client.get(f"/api/miniapp/uch/contacts/1{tail}")
                out[tail] = r.status
            return out
        finally:
            await client.close()

    statuses = asyncio.run(go())
    assert set(statuses.values()) == {404}, statuses
