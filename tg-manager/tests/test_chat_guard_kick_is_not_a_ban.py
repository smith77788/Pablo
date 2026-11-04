"""Исключение за непройденную капчу не превращается в вечный бан.

ЧТО БЫЛО. «Исключить» — это бан и сразу разбан, чтобы человек мог вернуться
и пройти капчу. Оба вызова стояли в одном try: сбой связи ровно на разбане
проглатывался, и человек, просто не успевший нажать «Я не бот», оставался
забаненным навсегда. Теперь разбан повторяется.
"""
from __future__ import annotations

import asyncio


class _Bot:
    def __init__(self, unban_failures: int):
        self.unban_failures = unban_failures
        self.calls: list[str] = []

    async def delete_message(self, chat_id, msg_id):
        self.calls.append("delete")

    async def ban_chat_member(self, chat_id, uid):
        self.calls.append("ban")

    async def unban_chat_member(self, chat_id, uid, only_if_banned=False):
        self.calls.append("unban")
        if self.unban_failures:
            self.unban_failures -= 1
            raise RuntimeError("Telegram недоступен")


def _punish(monkeypatch, bot, action):
    from services import chat_guard_runner as r

    async def _no_sleep(*_a):
        return None

    monkeypatch.setattr(r.asyncio, "sleep", _no_sleep)
    asyncio.run(r._punish_expired(None, bot, {"chat_id": -100, "user_id": 7,
                                              "captcha_msg_id": 1, "action": action}))


def test_kick_retries_the_unban(monkeypatch):
    bot = _Bot(unban_failures=1)
    _punish(monkeypatch, bot, "kick")
    assert bot.calls.count("unban") == 2, bot.calls


def test_ban_stays_a_ban(monkeypatch):
    bot = _Bot(unban_failures=0)
    _punish(monkeypatch, bot, "ban")
    assert "unban" not in bot.calls
