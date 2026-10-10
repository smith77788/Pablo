"""Гейт подписки не спамит в группы (продовый баг Мать-Дочка).

Скрины из прода: при массовом инвайте бот вместо тишины постил
«👋 Добро пожаловать! Для использования бота необходимо подписаться…» прямо в
чат — на каждое сообщение любого неподписанного участника. Только что
приглашённые люди уходили из-за спама уведомлениями.

Причина: SubscriptionGateMiddleware — концепт ЛИЧНОГО чата с ботом, но
срабатывал в группах/супергруппах тоже. Гейт должен показываться только в
private; в остальных чатах — молча пропускать.
"""
from __future__ import annotations

import asyncio

from aiogram.types import CallbackQuery, Message

from bot.middlewares import subscription_gate as SG


class _User:
    def __init__(self, uid): self.id = uid; self.is_bot = False


class _Chat:
    def __init__(self, ctype): self.type = ctype


def _inject(obj, name, fn):
    """Модели aiogram заморожены (pydantic frozen) — подменяем метод в обход."""
    object.__setattr__(obj, name, fn)


def _make_message(uid, chat_type):
    m = Message.model_construct(
        message_id=1, from_user=_User(uid), chat=_Chat(chat_type), text="привет")
    return m


def _make_callback(uid, data, chat_type="supergroup"):
    inner = _make_message(uid, chat_type)
    return CallbackQuery.model_construct(
        id="1", from_user=_User(uid), data=data, message=inner, chat_instance="x")


class _Bot:
    def __init__(self): self.member_status = "left"

    async def get_chat_member(self, chat_id, user_id):
        class _M:  # noqa
            status = self.member_status
        return _M()


def _run(event, calls, bot=None):
    async def handler(ev, data):
        calls.append("handler")
        return "passed"

    mw = SG.SubscriptionGateMiddleware()
    return asyncio.run(mw(handler, event, {"bot": bot or _Bot()}))


def _setup_gate():
    SG.set_gate_channels([{"channel_username": "@chan", "channel_title": "Chan"}])
    SG.set_gate_enabled(True)
    SG._gate_cache.clear()


def teardown_function(_):
    SG.set_gate_enabled(False)
    SG.set_gate_channels([])
    SG._gate_cache.clear()


def test_group_message_passes_through_without_posting_a_gate():
    """Главный регресс: в супергруппе гейт НЕ показывается, хендлер вызывается."""
    _setup_gate()
    calls = []
    # Ловим любую попытку отправить сообщение в чат.
    posted = []
    msg = _make_message(555, "supergroup")
    _inject(msg, "answer", lambda *a, **k: posted.append(a))
    _run(msg, calls)
    assert calls == ["handler"], "в группе гейт не должен блокировать"
    assert not posted, "гейт не должен постить сообщение в группу"


def test_group_variants_all_pass_through():
    for ctype in ("group", "supergroup", "channel"):
        _setup_gate()
        calls = []
        msg = _make_message(1, ctype)
        def _boom(*a, **k): raise AssertionError("posted in " + ctype)
        _inject(msg, "answer", _boom)
        _run(msg, calls)
        assert calls == ["handler"]


def test_private_chat_still_gated():
    """В личке гейт обязан работать — иначе сломали бы саму защиту."""
    _setup_gate()
    calls = []
    posted = []

    async def _answer(*a, **k):
        posted.append(a)

    msg = _make_message(777, "private")
    _inject(msg, "answer", _answer)
    result = _run(msg, calls)
    assert calls == [], "в личке неподписанный пользователь блокируется"
    assert posted, "в личке гейт должен показаться"
    assert result is None


def test_check_button_still_works_in_a_group():
    """Кнопку «проверить» на историческом гейт-сообщении в группе не ломаем:
    она редактирует своё сообщение, а не постит новое."""
    _setup_gate()
    bot = _Bot(); bot.member_status = "member"   # подписан — проверка пройдёт
    cb = _make_callback(888, "gate:check", chat_type="supergroup")
    answered = []
    edited = []

    async def _ans(*a, **k): answered.append(a)
    async def _edit(*a, **k): edited.append(a)
    _inject(cb, "answer", _ans)
    _inject(cb.message, "edit_text", _edit)
    calls = []
    _run(cb, calls, bot=bot)
    assert answered, "callback должен ответить"
    assert calls == [], "gate:check не должен пропускать дальше в хендлер"


def test_disabled_gate_never_interferes():
    SG.set_gate_enabled(False)
    calls = []
    msg = _make_message(2, "private")
    _run(msg, calls)
    assert calls == ["handler"]
