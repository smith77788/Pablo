"""check_alive: 'dead' только на 401, всё прочее не-ОК — 'unknown'.

Это ядро безопасности очистки флота. Удалять бота можно лишь по 'dead'
(токен отозван, getMe → 401). Сетевой сбой, 5xx и 429 обязаны давать
'unknown' — иначе недоступность Telegram унесла бы живых ботов (тот же
fail-open, что у is_account_quarantined). get_me для этого не годится: он
отдаёт None и на 401, и на обрыв сети, то есть смешивает ровно то, что
здесь нельзя смешивать.
"""
from __future__ import annotations

import asyncio

from services import bot_api


def _run(coro):
    return asyncio.run(coro)


def _patch_call(monkeypatch, response):
    async def _fake_call(session, token, method, **params):
        return response
    monkeypatch.setattr(bot_api, "_call", _fake_call)


def test_ok_is_alive(monkeypatch):
    _patch_call(monkeypatch, {"ok": True, "result": {"id": 1, "username": "b"}})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_ALIVE


def test_401_is_dead(monkeypatch):
    _patch_call(monkeypatch, {"ok": False, "error_code": 401,
                              "description": "Unauthorized"})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_DEAD


def test_unauthorized_text_is_dead(monkeypatch):
    # Код не 401, но текст говорит про отзыв — тоже мёртв.
    _patch_call(monkeypatch, {"ok": False, "error_code": 0,
                              "description": "Unauthorized: bot token revoked"})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_DEAD


def test_network_error_is_unknown_not_dead(monkeypatch):
    # _call ставит error_code=0 при недостижимом Telegram. Это НЕ повод удалять.
    _patch_call(monkeypatch, {"ok": False, "error_code": 0,
                              "description": "Network error after 3 retries"})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_UNKNOWN


def test_5xx_is_unknown(monkeypatch):
    _patch_call(monkeypatch, {"ok": False, "error_code": 502,
                              "description": "Bad Gateway"})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_UNKNOWN


def test_429_is_unknown(monkeypatch):
    _patch_call(monkeypatch, {"ok": False, "error_code": 429,
                              "description": "Too Many Requests"})
    assert _run(bot_api.check_alive(None, "tok")) == bot_api.BOT_UNKNOWN


def test_batch_preserves_order_and_maps_exception_to_unknown(monkeypatch):
    # Разные исходы по токену + упавший вызов → 'unknown', а не «мёртв».
    async def _fake_check(session, token):
        if token == "boom":
            raise RuntimeError("executor died")
        return {"dead": bot_api.BOT_DEAD, "net": bot_api.BOT_UNKNOWN}.get(
            token, bot_api.BOT_ALIVE)
    monkeypatch.setattr(bot_api, "check_alive", _fake_check)
    out = _run(bot_api.batch_check_alive(None, ["ok1", "dead", "net", "boom"]))
    assert out == [bot_api.BOT_ALIVE, bot_api.BOT_DEAD,
                   bot_api.BOT_UNKNOWN, bot_api.BOT_UNKNOWN]
