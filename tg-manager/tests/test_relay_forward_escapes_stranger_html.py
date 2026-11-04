"""Пересылка оператору: имя и текст постороннего — экранированный текст.

Сообщение подписчика бота владельца уходило в чат владельца с parse_mode=HTML
как есть. Подписчик с именем или текстом `<a href="https://phish">Подтвердите
оплату</a>` получал кликабельную ссылку в чате владельца от имени платформы,
а обычное «цена < 100» ломало разметку: Telegram отклонял сообщение, и оно
терялось. Тест падает без фикса.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import auto_responder as ar

EVIL = '<a href="https://phish.example">Подтвердите оплату</a> цена < 100'


def _pool(relay: bool):
    class _P:
        async def fetchrow(self, query, *args):
            if "FROM managed_bots" in query:
                return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                        "added_by": 1, "username": "b", "first_name": "b",
                        "relay_enabled": relay}
            return None

        async def fetch(self, query, *args):
            return []

        async def fetchval(self, query, *args):
            return None

        async def execute(self, query, *args):
            return "UPDATE 1"
    return _P()


def test_forwarded_stranger_text_is_escaped():
    relay = False  # пересылка «без релея» — путь, которым идут сообщения
    sent: list[str] = []

    class _Main:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return None

    upd = {"update_id": 101, "message": {
        "message_id": 1, "text": EVIL,
        "from": {"id": 7001, "is_bot": False, "first_name": "<b>Админ</b>"},
        "chat": {"id": 7001, "type": "private"}}}

    async def _call(session, token, method, **params):
        return {"ok": True, "result": {}}

    patches = [
        patch.object(ar.bot_api, "_call", _call),
        patch("database.db.get_update_offset", AsyncMock(return_value=100)),
        patch("database.db.set_update_offset", AsyncMock()),
        patch("database.db.get_active_auto_replies", AsyncMock(return_value=[])),
        patch("database.db.get_active_funnels", AsyncMock(return_value=[])),
        patch("database.db.get_active_automation_rules", AsyncMock(return_value=[])),
        patch("database.db.get_active_experiment", AsyncMock(return_value=None)),
        patch("database.db.upsert_users", AsyncMock(return_value=[])),
        patch("database.db.get_or_create_relay_session", AsyncMock(return_value=1)),
        patch("database.db.save_relay_message", AsyncMock()),
        patch.object(ar, "_record_bot_ok", AsyncMock()),
        patch.object(ar, "_record_bot_error", AsyncMock()),
        patch("services.brand_injection.is_free_tier", AsyncMock(return_value=False)),
    ]
    for p in patches:
        p.start()
    try:
        asyncio.run(ar._process_bot(_pool(relay), object(), 555,
                                    "123456789:AAHrealtokenlooking-value_0123456789ab",
                                    main_bot=_Main(), updates=[upd]))
    finally:
        for p in reversed(patches):
            p.stop()
    fwd = [t for t in sent if "ответить пользователю" in t]
    assert fwd, sent
    assert "<a href" not in fwd[0] and "<b>Админ</b>" not in fwd[0]
    assert "&lt;a href=" in fwd[0] and "цена &lt; 100" in fwd[0]
