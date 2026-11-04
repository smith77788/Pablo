"""Бан владельца закрывает бота, мини-апп и личные ключи REST API.

Админ ставил `platform_users.is_banned`, пользователь получал «Ваш аккаунт
заблокирован» — и продолжал работать: флаг не читал никто. Забаненный за спам
владелец дальше запускал рассылки и управлял аккаунтами.

Все тесты падают без фикса.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import platform_ban

BANNED = 880011


class _Pool:
    def __init__(self, banned: set[int]):
        self.banned = banned
        self.calls = 0

    async def fetchval(self, q, *a):
        if "is_banned" in q:
            self.calls += 1
            return a[0] in self.banned
        return 0

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def execute(self, q, *a):
        return "OK"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    platform_ban.forget()
    yield
    platform_ban.forget()


async def test_is_banned_caches_and_forget_resets():
    pool = _Pool({BANNED})
    assert await platform_ban.is_banned(pool, BANNED)
    assert await platform_ban.is_banned(pool, BANNED)
    assert pool.calls == 1
    pool.banned.clear()
    platform_ban.forget(BANNED)
    assert not await platform_ban.is_banned(pool, BANNED)


async def test_admin_is_never_banned(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", str(BANNED))
    assert not await platform_ban.is_banned(_Pool({BANNED}), BANNED)


async def test_bot_middleware_stops_banned_user():
    from aiogram.types import CallbackQuery, Chat, Message, User
    from bot.middlewares.ban_guard import BanGuardMiddleware

    mw = BanGuardMiddleware()
    called: list = []

    async def handler(event, data):
        called.append(event)

    user = User(id=BANNED, is_bot=False, first_name="x")
    msg = Message(message_id=1, date=0, chat=Chat(id=BANNED, type="private"),
                  from_user=user, text="/start")
    answered: list = []

    async def _answer(self, *a, **kw):
        answered.append(a)

    pool = _Pool({BANNED})
    import unittest.mock as um
    with um.patch.object(Message, "answer", _answer), \
            um.patch.object(CallbackQuery, "answer", _answer):
        await mw(handler, msg, {"pool": pool})
        cb = CallbackQuery(id="1", from_user=user, chat_instance="c", data="x")
        await mw(handler, cb, {"pool": pool})
        assert called == []
        assert len(answered) == 2

        pool.banned.clear()
        platform_ban.forget()
        await mw(handler, msg, {"pool": pool})
        assert called == [msg]


def test_main_registers_ban_guard():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    for kind in ("message", "callback_query", "inline_query"):
        assert f"dp.{kind}.outer_middleware(ban_guard)" in src


def test_miniapp_rejects_banned_session(monkeypatch):
    from services import mini_app_api as M

    monkeypatch.setattr(M, "_get_uid", lambda r: BANNED)
    app = web.Application()
    M.setup_routes(app, _Pool({BANNED}))

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/miniapp/accounts")
            return resp.status, await resp.text()

    status, text = asyncio.run(go())
    assert status == 403, (status, text[:200])
    assert "заблокирован" in text


async def test_rest_api_key_of_banned_owner_is_refused(monkeypatch):
    from services import api_keys, rest_api

    async def _auth(pool, key):
        return {"owner_id": BANNED, "scopes": ["read"], "key_id": 1}

    monkeypatch.setattr(api_keys, "authenticate", _auth)
    monkeypatch.setattr(rest_api, "_check_auth", lambda r: False)
    req = SimpleNamespace(headers={"X-Api-Key": "k" * 40})
    assert await rest_api._authorize(req, _Pool({BANNED})) is None
    platform_ban.forget()
    assert await rest_api._authorize(req, _Pool(set())) is not None
