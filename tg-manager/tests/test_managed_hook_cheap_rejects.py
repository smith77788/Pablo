"""Вебхук подключённых ботов отбивает мусор, не трогая базу.

POST /tgbot/hook/{bot_id} открыт интернету, а проверка секрета стоила запроса
в базу и расшифровки токена на КАЖДЫЙ запрос — простой способ занять пул
соединений. Теперь заголовок не той формы — 401 сразу, а частые промахи с
одного адреса — 429 без базы. Тест падает без фикса (база дёргалась).
"""
from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import managed_bot_webhooks as mbw
from services import security


class _Pool:
    def __init__(self):
        self.calls = 0

    async def fetchrow(self, *a):
        self.calls += 1
        return {"token": "123456789:AAEhBP0av28bGtUcLzZ2Dz5UudzRo7PkLg0"}


async def _client(pool):
    app = web.Application()
    mbw.add_routes(app, pool)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def test_malformed_secret_never_reaches_db():
    pool = _Pool()
    c = await _client(pool)
    try:
        for h in ({}, {"X-Telegram-Bot-Api-Secret-Token": "x"},
                  {"X-Telegram-Bot-Api-Secret-Token": "Z" * 64}):
            r = await c.post("/tgbot/hook/5", json={}, headers=h)
            assert r.status == 401
        assert pool.calls == 0
    finally:
        await c.close()


async def test_repeated_wrong_secrets_are_throttled_without_db(monkeypatch):
    monkeypatch.setattr(security, "_rate_limiter", security._RateLimiter())
    pool = _Pool()
    c = await _client(pool)
    try:
        wrong = {"X-Telegram-Bot-Api-Secret-Token": "a" * 64}
        codes = [(await c.post("/tgbot/hook/5", json={}, headers=wrong)).status
                 for _ in range(mbw._HOOK_FAIL_MAX + 5)]
        assert codes[:mbw._HOOK_FAIL_MAX] == [401] * mbw._HOOK_FAIL_MAX
        assert set(codes[mbw._HOOK_FAIL_MAX:]) == {429}
        assert pool.calls == mbw._HOOK_FAIL_MAX
    finally:
        await c.close()
