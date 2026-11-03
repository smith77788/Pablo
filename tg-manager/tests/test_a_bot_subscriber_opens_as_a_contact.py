"""Подписчик бота открывается карточкой контакта.

Живой обход мини-аппа (03.11.2026): в «Новых подписчиках» строку нельзя было
нажать — ни открыть человека, ни завести сделку. Теперь нажатие находит или
заводит контакт одной дверью (ensure_contact_by_telegram_id) и открывает его
карточку. Скоуп: бот владельца и подписчик ЭТОГО бота — иначе по чужому
bot_id можно было бы завести карточку на постороннего человека.
"""
from __future__ import annotations

import asyncio

from tests.miniapp_source import api_function, js_function


class _Pool:
    def __init__(self, owned: bool):
        self.owned = owned
        self.asked: list[tuple] = []

    async def fetchrow(self, sql, *a):
        if "FROM bot_users bu" in sql and "mb.added_by=$3" in sql:
            self.asked.append(a)
            return {"username": "ivan", "first_name": "Иван"} if self.owned else None
        return None

    async def fetch(self, *a, **k): return []
    async def fetchval(self, *a, **k): return 0
    async def execute(self, *a, **k): return "OK"


def _post(monkeypatch, owned, body):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from services import mini_app_api
    from services.contacts_hub import repository

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)
    made: list[tuple] = []

    async def _ensure(pool, owner, tg, **k):
        made.append((owner, tg, k.get("first_name")))
        return "c0ffee00-0000-0000-0000-000000000001"

    monkeypatch.setattr(repository, "ensure_contact_by_telegram_id", _ensure)
    pool = _Pool(owned)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, pool)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/api/miniapp/bot_user/contact", json=body)
            return r.status, await r.json()
        finally:
            await client.close()

    status, d = asyncio.run(_go())
    return status, d, made, pool


def test_own_subscriber_becomes_a_contact(monkeypatch):
    status, d, made, pool = _post(monkeypatch, True, {"bot_id": 7012345678, "user_id": 555})
    assert status == 200 and d["contact_id"].startswith("c0ffee00")
    assert made == [(777, 555, "Иван")]
    assert pool.asked == [(7012345678, 555, 777)], "скоуп владельца не в запросе"


def test_foreign_bot_is_refused(monkeypatch):
    status, d, made, _ = _post(monkeypatch, False, {"bot_id": 7012345678, "user_id": 555})
    assert status == 404 and made == []


def test_list_rows_are_doors():
    assert "bu.bot_id" in api_function("new_users"), "в строке нет bot_id — дверь не построить"
    body = js_function("openNewUsers")
    assert "openSubscriberCard(" in body
    assert "openContactDetail(r.contact_id)" in js_function("openSubscriberCard")
