"""Экран «Тариф и оплата» знает купленный тариф и его лимит аккаунтов.

Живой обход мини-аппа (03.11.2026): у любого платного владельца экран писал
«Платный · ПРО» и «Telegram-аккаунтов: ∞». Тарифы же различаются именно
лимитом флота (services/tiers.py: Старт — до 10, Про — до 50, Бизнес — без
лимита), и подписчик «Старт» читал обещание, которого у него нет. Причина —
ручка /api/miniapp/subscription отдавала только уровень доступа (free/paid), а
тариф подписки не выбирала вовсе.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from tests.miniapp_source import js_function


class _Pool:
    def __init__(self, tier):
        self.tier = tier

    async def fetchrow(self, sql, *a):
        if "FROM subscriptions" in sql:
            return {"plan": "paid", "expires_at": datetime.now(timezone.utc) + timedelta(days=9),
                    "tier": self.tier}
        return None

    async def fetchval(self, *a, **k):
        return None

    async def fetch(self, *a, **k):
        return []

    async def execute(self, *a, **k):
        return "OK"


def _get(monkeypatch, tier):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from bot.utils import subscription
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: 777)

    async def _plan(pool, uid):
        return "paid"

    monkeypatch.setattr(subscription, "get_plan", _plan)

    async def _go():
        app = web.Application()
        mini_app_api.setup_routes(app, _Pool(tier))
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.get("/api/miniapp/subscription")
            return r.status, await r.json()
        finally:
            await client.close()

    return asyncio.run(_go())


def test_start_tier_is_named_with_its_cap(monkeypatch):
    status, d = _get(monkeypatch, "start")
    assert status == 200 and d["tier"] == "start" and d["tier_name"] == "Старт", d
    assert d["account_cap"] == 10, d


def test_business_tier_is_unlimited(monkeypatch):
    status, d = _get(monkeypatch, "business")
    assert d["tier"] == "business" and d["account_cap"] is None, d


def test_subscription_without_tier_says_no_tier(monkeypatch):
    # Подписка до тарифов / триал / ручная выдача — лимита нет, тарифа нет.
    status, d = _get(monkeypatch, None)
    assert d["plan"] == "paid" and d["tier"] is None and d["account_cap"] is None, d


def test_screen_shows_the_tier_not_a_fixed_word():
    body = js_function("openBilling")
    assert "badge.textContent='ПРО'" not in body, "любому платному снова пишется «ПРО»"
    assert "sub.tier_name" in body and "sub.account_cap" in body
