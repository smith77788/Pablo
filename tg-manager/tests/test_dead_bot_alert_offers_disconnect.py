"""Алерт «токен отозван» обязан предлагать ОТКЛЮЧИТЬ бота, не только заменить.

Просьба владельца (16.11.2026): «система видит, что бот заблокирован, но не
предлагает отключить его». Раньше уведомление о мёртвом токене звало только
получить новый токен у @BotFather. Теперь у него есть кнопка «🗑 Отключить
бота» (owner-scoped, тот же мягкий путь, что «Удалить» в карточке — подписчики
и история сохраняются), и текст называет эту опцию.
"""
from __future__ import annotations

import asyncio

import pytest

from services import bot_health as bh


def test_unauthorized_alert_mentions_disconnect_option():
    text = bh.build_alert("@deadbot", bh.UNAUTHORIZED, 3)
    assert "отключите его" in text.lower()
    # И по-прежнему путь замены токена.
    assert "@BotFather" in text


def test_non_unauthorized_alert_has_no_disconnect_line():
    text = bh.build_alert("@bot", bh.CONFLICT, 2)
    assert "отключите его" not in text.lower()


def test_notify_attaches_disconnect_button_only_for_unauthorized(monkeypatch):
    from services import auto_responder as ar
    from database import db

    rows = [
        {"bot_id": 11, "added_by": 7, "username": "deadbot",
         "first_name": "Dead", "last_error": "unauthorized", "fail_streak": 3},
        {"bot_id": 22, "added_by": 7, "username": "conflbot",
         "first_name": "Confl", "last_error": "conflict", "fail_streak": 2},
    ]

    class _Pool:
        async def fetch(self, *a, **k):
            return rows
        async def execute(self, *a, **k):
            return "UPDATE 1"

    seen = {}

    async def _notify(pool, bot, owner, pref, text, reply_markup=None, *, dedup_key=None):
        seen[dedup_key] = reply_markup
        return True

    monkeypatch.setattr(db, "notify_if_enabled", _notify)
    # decide_alert должен пропустить обе строки.
    monkeypatch.setattr(bh, "decide_alert", lambda *a, **k: True)

    sent = asyncio.run(ar.notify_broken_bots(_Pool(), object()))
    assert sent == 2
    # У отозванного токена — есть клавиатура с кнопкой; у конфликта — нет.
    assert seen["bot_broken:11"] is not None, "мёртвый бот без кнопки отключения"
    assert seen["bot_broken:22"] is None, "конфликту кнопка отключения не нужна"
    # Кнопка ведёт в BotCb(action='delete').
    kb = seen["bot_broken:11"]
    packed = kb.inline_keyboard[0][0].callback_data
    assert packed.startswith("bot:delete:11") or ":delete:" in packed
