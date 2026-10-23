"""Регресс: Reg-Checker писал «Telethon-проверка недоступна. Нет активных
аккаунтов с сессией», хотя у владельца подключён целый флот.

Первопричина: `_enrich_metadata` брал owner_id из `sent_msg.from_user` — но
`sent_msg` это ответ самого бота, его автор — бот. Аккаунты искались по id
бота, их, разумеется, не было. Теперь owner_id передаётся явно из входящего
сообщения пользователя.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 555001
BOT_ID = 777999


def _load_reg_checker():
    path = os.path.join(ROOT, "bot", "handlers", "reg_checker.py")
    spec = importlib.util.spec_from_file_location("_reg_checker_enrich_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _User:
    def __init__(self, uid):
        self.id = uid


class _BotMessage:
    """Сообщение, отправленное ботом: from_user — сам бот."""

    def __init__(self):
        self.from_user = _User(BOT_ID)
        self.chat = _User(OWNER)
        self.text = "📅 Дата: ..."
        self.caption = None
        self.bot = None
        self.edited: str | None = None

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edited = text


def _run(mod, monkeypatch, *, has_session: bool):
    seen: dict = {}

    async def fake_full_info(pool, owner_id, peer, pool_name=None):
        seen["owner_id"] = owner_id
        return None

    async def fake_has_session(pool, owner_id, pool_name=None):
        seen["has_owner"] = owner_id
        return has_session

    monkeypatch.setattr(mod.rc, "get_entity_full_info", fake_full_info)
    monkeypatch.setattr(mod.rc, "has_active_session", fake_has_session)
    msg = _BotMessage()
    asyncio.run(mod._enrich_metadata(
        msg, None, OWNER, 123, "bot", "Bot", "bot_user", "bot_user", None, None,
    ))
    return seen, msg


def test_enrich_uses_owner_not_bot_id(monkeypatch):
    mod = _load_reg_checker()
    seen, _ = _run(mod, monkeypatch, has_session=False)
    assert seen["owner_id"] == OWNER
    assert seen["has_owner"] == OWNER


def test_no_false_empty_fleet_message_when_sessions_exist(monkeypatch):
    mod = _load_reg_checker()
    _, msg = _run(mod, monkeypatch, has_session=True)
    assert msg.edited and "Нет активных аккаунтов" not in msg.edited


def test_empty_fleet_message_when_really_empty(monkeypatch):
    mod = _load_reg_checker()
    _, msg = _run(mod, monkeypatch, has_session=False)
    assert msg.edited and "Нет активных аккаунтов" in msg.edited
