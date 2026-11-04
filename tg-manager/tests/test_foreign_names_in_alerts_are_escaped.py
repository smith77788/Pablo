"""Имена посторонних в уведомлениях админам и владельцам — экранированный текст.

Уведомление «новый пользователь» (имя пишет сам пришедший) и алерт нотариуса
(название чужого канала задаёт его админ) уходили с parse_mode=HTML как есть:
«<a href=…>» становилась ссылкой в чате админа, а «<» ломал разметку, и
Telegram уведомление не доставлял. Тесты падают без фикса.
"""
from __future__ import annotations

from services import notary_observer
from bot.handlers import admin

EVIL = '<a href="https://phish.example">жми</a> & <'


class _Bot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **k):
        self.sent.append(text)


async def test_new_platform_user_name_is_escaped(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "42")

    class _Pool:
        async def fetchval(self, *a):
            return 1

        async def execute(self, *a):
            return "OK"

    bot = _Bot()
    await admin.notify_new_platform_user(bot, _Pool(), 7, None, EVIL)
    assert bot.sent, "уведомление не отправлено"
    assert "<a href" not in bot.sent[0] and "&lt;a href=" in bot.sent[0]


async def test_notary_alert_escapes_channel_title():
    from services import notary

    class _Pool:
        async def fetchrow(self, *a):
            return {"owner_id": 5, "channel_ref": "@x", "channel_title": EVIL,
                    "verdict": notary.V_EARLY}

    bot = _Bot()
    await notary_observer._maybe_alert(_Pool(), bot, 1, None)
    assert bot.sent
    assert "<a href" not in bot.sent[0] and "&lt;a href=" in bot.sent[0]
