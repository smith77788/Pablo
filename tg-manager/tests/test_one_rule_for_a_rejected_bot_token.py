"""«Telegram отказал по токену бота» — одно правило для опроса и рассылки.

ЧТО БЫЛО. Опрос ботов (auto_responder) и рассылка понимали «токен мёртв»
только как 401, а проверка перед запуском (bot_api.token_state) — как 401
или 404. На 404 (бота удалили, токен перепечатан с ошибкой) опрос долбил
Telegram каждые 10 секунд без отступа, а рассылка добивала всю аудиторию
одной и той же ошибкой. Удаление ботов (check_alive) намеренно остаётся
на одном 401: 404 на всех ботах разом даёт и неверный адрес API.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.parametrize("code,desc,expected", [
    (401, "Unauthorized", True),
    (404, "Not Found", True),
    (0, "Unauthorized", True),
    (0, "Network error after 3 retries", False),
    (502, "Bad Gateway", False),
    (429, "Too Many Requests", False),
    (400, "Bad Request: chat not found", False),
])
def test_token_rejected(code, desc, expected):
    from services import bot_api
    assert bot_api.token_rejected(code, desc) is expected


def test_broadcast_stops_on_a_deleted_bot():
    from services import bot_api
    assert bot_api.classify_send_error(404, "Not Found") == "unauthorized"


def test_polling_backs_off_on_a_deleted_bot(monkeypatch):
    from services import auto_responder as ar

    bot_id = 880099001

    async def _call(http, token, method, **kw):
        return {"ok": False, "error_code": 404, "description": "Not Found"}

    async def _offset(pool, bid):
        return 10

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ar.bot_api, "_call", _call)
    monkeypatch.setattr(ar.db, "get_update_offset", _offset)
    monkeypatch.setattr(ar, "_record_bot_error", _noop)
    ar._dead_token_cooldown.pop(bot_id, None)
    try:
        asyncio.run(ar._process_bot(None, None, bot_id, "1:x"))
        assert bot_id in ar._dead_token_cooldown, (
            "опрос удалённого бота (404) не ушёл на паузу — Telegram долбят каждые 10 с")
    finally:
        ar._dead_token_cooldown.pop(bot_id, None)
