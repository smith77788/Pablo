"""Рассылка не помечается провалом из-за сбоя связи на проверке токена.

ЧТО БЫЛО. Перед отправкой `broadcaster.run` зовёт getMe и на ЛЮБОМ неуспехе
ставил рассылке 'failed' навсегда: «токен отозван». Но getMe не проходит и на
сетевой ошибке, 5xx, флуде. Не отправлено было ничего, а рассылка — и
отложенная, и сетевая — хоронилась минутой недоступности Telegram.

Теперь провал — только на 401/404; на сбое связи проверка повторяется, а если
Telegram так и не ответил, статус не трогается и рассылку поднимет докатка.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    async def fetch(self, *a, **k):
        return []

    async def fetchrow(self, *a, **k):
        return None

    async def fetchval(self, query, *a, **k):
        # Владелец бота есть — иначе рассылка закрывается раньше проверки токена.
        return 1 if "added_by FROM managed_bots" in query else None

    async def execute(self, *a, **k):
        return "UPDATE 0"


def _run_broadcast(monkeypatch, replies):
    from database import db
    from services import bot_api, broadcaster

    marks: list[str] = []
    replies = list(replies)

    async def _call(session, token, method, **params):
        assert method == "getMe"
        return replies.pop(0) if replies else {"ok": False, "error_code": 0}

    async def _update(pool, bid, s, f, status):
        marks.append(status)

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(bot_api, "_call", _call)
    monkeypatch.setattr(db, "update_broadcast", _update)
    monkeypatch.setattr(broadcaster.asyncio, "sleep", _no_sleep)
    asyncio.run(broadcaster.run(_Pool(), object(), 1, "123456:" + "A" * 35, 5, "Привет"))
    return marks, replies


NET = {"ok": False, "error_code": 0, "description": "Network error after 3 retries"}
BAD_GW = {"ok": False, "error_code": 502, "description": "Bad Gateway"}


@pytest.mark.parametrize("reply", [NET, BAD_GW])
def test_outage_does_not_fail_the_broadcast(monkeypatch, reply):
    marks, _ = _run_broadcast(monkeypatch, [reply] * 10)
    assert "failed" not in marks, f"сбой связи пометил рассылку провалом: {marks}"


def test_revoked_token_still_fails_the_broadcast(monkeypatch):
    marks, _ = _run_broadcast(
        monkeypatch, [{"ok": False, "error_code": 401, "description": "Unauthorized"}] * 2)
    assert marks == ["failed"], marks


def test_token_state_classifies_replies(monkeypatch):
    from services import bot_api

    async def go(reply):
        async def _call(session, token, method, **params):
            return reply
        monkeypatch.setattr(bot_api, "_call", _call)
        return await bot_api.token_state(None, "t")

    assert asyncio.run(go({"ok": True, "result": {"id": 1}})) == "ok"
    assert asyncio.run(go({"ok": False, "error_code": 404})) == "revoked"
    assert asyncio.run(go(NET)) == "transient"
    assert asyncio.run(go({"ok": False, "error_code": 429})) == "transient"


def test_own_session_is_closed_when_the_dedup_read_fails(monkeypatch):
    """Рассылка, отложенная из-за недоступной базы, не оставляет открытую
    HTTP-сессию (раньше этот выход её не закрывал)."""
    import aiohttp

    from database import db
    from services import bot_api, broadcaster

    sessions: list = []
    real = aiohttp.ClientSession

    def _tracked(*a, **k):
        s = real(*a, **k)
        sessions.append(s)
        return s

    async def _me(session, token):
        return {"id": 1}

    async def _no_dedup(pool, bid):
        raise RuntimeError("база недоступна")

    async def _audience(pool, bot_id):
        return [1, 2]

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(broadcaster.aiohttp, "ClientSession", _tracked)
    monkeypatch.setattr(bot_api, "get_me", _me)
    monkeypatch.setattr(db, "get_broadcast_delivered_ids", _no_dedup)
    monkeypatch.setattr(db, "get_audience_user_ids", _audience)
    monkeypatch.setattr(broadcaster.asyncio, "sleep", _no_sleep)
    asyncio.run(broadcaster.run(_Pool(), None, 1, "123456:" + "A" * 35, 5, "Привет"))
    assert sessions and all(s.closed for s in sessions), "своя сессия рассылки не закрыта"
