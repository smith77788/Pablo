"""chat_guard самоактивируется, если бот уже админ (продовый баг Мать-Дочка).

Скрины из прода: «X покинул(а) группу» висят неудалёнными, хотя бот — админ с
полными правами. Причина: чистка работает только для чата «под охраной»
(guard_chats.is_active), а активация висела ТОЛЬКО на событии my_chat_member в
момент повышения бота. При массовом инвайте бота промоутят программно
userbot-ом, либо апдейт теряется при рестарте — строки нет, и on_service_message
молча выходит.

Фикс: пришло служебное сообщение, строки нет, но бот в чате админ с правом
удаления — ставим чат под охрану на месте и чистим.
"""
from __future__ import annotations

import asyncio

import bot.handlers.chat_guard as H
from services import chat_guard as cg


class _Chat:
    def __init__(self, cid=-100, ctype="supergroup"):
        self.id = cid; self.type = ctype; self.title = "Chat"; self.username = None


class _Member:
    def __init__(self, status, can_delete):
        self.status = status; self.can_delete_messages = can_delete


class _Bot:
    def __init__(self, status="administrator", can_delete=True):
        self.id = 42
        self._m = _Member(status, can_delete)
        self.calls = 0

    async def get_chat_member(self, chat_id, user_id):
        self.calls += 1
        return self._m


def test_lazy_activation_when_bot_is_admin_with_delete_rights():
    bot = _Bot("administrator", True)
    registered = {}

    async def _fake_register(pool, owner, chat_id, title="", username=""):
        registered["chat_id"] = chat_id
        return {"is_active": True, "settings": dict(cg.DEFAULT_SETTINGS)}

    orig = cg.register_chat
    cg.register_chat = _fake_register
    try:
        res = asyncio.run(H._lazy_activate_if_admin(bot, object(), _Chat(-500)))
    finally:
        cg.register_chat = orig
    assert res is not None
    assert registered["chat_id"] == -500


def test_no_activation_when_bot_is_admin_but_cannot_delete():
    """Полные права КРОМЕ удаления — активировать бессмысленно, чистить нечем."""
    bot = _Bot("administrator", False)
    assert asyncio.run(H._lazy_activate_if_admin(bot, object(), _Chat())) is None


def test_no_activation_when_bot_is_only_a_member():
    bot = _Bot("member", False)
    assert asyncio.run(H._lazy_activate_if_admin(bot, object(), _Chat())) is None


def test_lazy_activation_survives_api_failure():
    class _BoomBot(_Bot):
        async def get_chat_member(self, chat_id, user_id):
            raise RuntimeError("network")
    assert asyncio.run(H._lazy_activate_if_admin(_BoomBot(), object(), _Chat())) is None


# ── Интеграция: служебное сообщение чистится после самоактивации ────────────

class _Msg:
    def __init__(self, content_type="left_chat_member"):
        self.chat = _Chat()
        self.content_type = content_type
        self.new_chat_members = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _run_service(msg, bot, *, guarded, lazy_ok):
    async def _is_guarded(pool, chat_id):
        return {"settings": dict(cg.DEFAULT_SETTINGS)} if guarded else None

    async def _lazy(bot_, pool, chat):
        return {"settings": dict(cg.DEFAULT_SETTINGS)} if lazy_ok else None

    o1, o2 = cg.is_guarded, H._lazy_activate_if_admin
    cg.is_guarded = _is_guarded
    H._lazy_activate_if_admin = _lazy
    try:
        asyncio.run(H.on_service_message(msg, bot, object()))
    finally:
        cg.is_guarded = o1
        H._lazy_activate_if_admin = o2


def test_leave_message_deleted_after_lazy_activation():
    """Главный регресс: строки нет, но бот админ → активируем и удаляем «покинул»."""
    msg = _Msg("left_chat_member")
    _run_service(msg, _Bot(), guarded=False, lazy_ok=True)
    assert msg.deleted is True


def test_leave_message_kept_when_bot_not_admin():
    msg = _Msg("left_chat_member")
    _run_service(msg, _Bot("member", False), guarded=False, lazy_ok=False)
    assert msg.deleted is False


def test_already_guarded_chat_still_cleans():
    msg = _Msg("left_chat_member")
    _run_service(msg, _Bot(), guarded=True, lazy_ok=False)
    assert msg.deleted is True


def test_clean_leave_can_be_disabled_by_settings():
    """Самоактивация ставит настройки «из коробки», но если оператор ПОЗЖЕ
    выключил clean_leave — уважать это (иначе тумблер был бы фиктивным)."""
    msg = _Msg("left_chat_member")

    async def _is_guarded(pool, chat_id):
        s = dict(cg.DEFAULT_SETTINGS); s["clean_leave"] = False
        return {"settings": s}

    orig = cg.is_guarded
    cg.is_guarded = _is_guarded
    try:
        asyncio.run(H.on_service_message(msg, _Bot(), object()))
    finally:
        cg.is_guarded = orig
    assert msg.deleted is False
