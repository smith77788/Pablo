"""Карточка рассылки открывается только хозяину бота.

`bc:detail:<bot_id>:<id>` и `bc:bc_stat:…` искали рассылку по паре
(id, bot_id), взятой из кнопки, и не проверяли, чей это бот. Кнопку можно
подделать, и тогда чужой пользователь читал текст чужой рассылки и её
статистику. Теперь сначала `db.get_bot(..., uid)`: чужой бот — «не найдена»,
рассылка из базы даже не читается.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("handler", ["cb_detail", "cb_bc_stat"])
def test_foreign_bot_broadcast_is_not_shown(monkeypatch, handler):
    from bot.callbacks import BroadcastCb
    from bot.handlers import broadcast as h

    asked: list[str] = []
    answers: list[str] = []

    async def fake_get_bot(pool, bot_id, uid, **k):
        asked.append(f"bot:{bot_id}:{uid}")
        return None

    async def fake_get_broadcast(pool, bc_id, bot_id=None):
        asked.append("broadcast")
        return {"id": bc_id, "message_text": "секрет", "status": "done"}

    async def answer(text=None, **k):
        answers.append(text or "")

    monkeypatch.setattr(h.db, "get_bot", fake_get_bot)
    monkeypatch.setattr(h.db, "get_broadcast", fake_get_broadcast)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=answer, message=None)
    asyncio.run(getattr(h, handler)(cb, BroadcastCb(action="detail", bot_id=777, broadcast_id=5), None))
    assert asked == ["bot:777:42"]
    assert answers and "не найдена" in answers[0]
