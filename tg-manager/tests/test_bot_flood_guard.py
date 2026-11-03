"""Антифлуд бота: личные сообщения и кнопки сверх лимита отбрасываются.

Раньше ограничения не было вовсе: один скрипт заваливал обработчики (база,
Telegram, ИИ) для всех владельцев, а фразу входа в админку перебирали без
счёта. Тест падает без фикса (модуля и регистрации в main.py не было).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiogram.types import Chat, Message, User

from bot.middlewares.flood_guard import FloodGuardMiddleware


def _msg(uid: int, chat_type: str = "private") -> Message:
    return Message(message_id=1, date=0, text="x",
                   chat=Chat(id=uid if chat_type == "private" else -100, type=chat_type),
                   from_user=User(id=uid, is_bot=False, first_name="u"))


import pytest


@pytest.fixture(autouse=True)
def _no_admins(monkeypatch):
    # Другие тесты кладут id в ADMIN_IDS — админов антифлуд не ограничивает.
    monkeypatch.delenv("ADMIN_IDS", raising=False)


def test_sliding_window_limits_one_user_only():
    g = FloodGuardMiddleware(max_events=5, window=10)
    assert all(g.allow(1, now=100.0 + i * 0.1) for i in range(5))
    assert not g.allow(1, now=101.0)
    assert g.allow(2, now=101.0), "чужой лимит соседа не задевает"
    assert g.allow(1, now=111.0), "окно прошло — снова можно"


def test_admins_are_not_limited(monkeypatch):
    monkeypatch.setenv("ADMIN_IDS", "7")
    g = FloodGuardMiddleware(max_events=5, window=10)
    assert all(g.allow(7, now=1.0) for _ in range(50))


def test_middleware_drops_private_flood_but_not_groups():
    g = FloodGuardMiddleware(max_events=5, window=60)
    calls = []

    async def handler(event, data):
        calls.append(event.chat.type)
        return "ok"

    async def go():
        sent = []

        async def fake_answer(self, *a, **k):
            sent.append(a)
        Message.answer = fake_answer  # type: ignore[method-assign]
        for _ in range(20):
            await g(handler, _msg(1), {})
        for _ in range(20):
            await g(handler, _msg(1, "supergroup"), {})
        return sent

    orig = Message.answer
    try:
        sent = asyncio.run(go())
    finally:
        Message.answer = orig  # type: ignore[method-assign]
    assert calls.count("private") == 5
    assert calls.count("supergroup") == 20, "модератор групп видит всех"
    assert len(sent) == 1, "предупреждение — один раз за окно"


def test_memory_is_bounded():
    g = FloodGuardMiddleware(max_events=5, window=1, max_users=100)
    for uid in range(1000):
        g.allow(uid, now=float(uid))
    assert len(g._hits) <= 101


def test_registered_first_in_main():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    i_flood = src.index("dp.message.outer_middleware(flood_guard)")
    assert i_flood < src.index("dp.message.outer_middleware(gate_middleware)")
    assert "dp.callback_query.outer_middleware(flood_guard)" in src
