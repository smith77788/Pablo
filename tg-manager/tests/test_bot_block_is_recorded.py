"""Заблокировавший бота человек перестаёт быть адресатом рассылок.

ЧТО БЫЛО. Telegram сам шлёт обновление `my_chat_member`, когда человек
блокирует бота: это обновление входит в набор по умолчанию, отдельно его
просить не надо. Продукт выбрасывал такой апдейт не глядя — обрабатывались
только сообщения и тапы по кнопкам.

Последствия: `bot_users.is_blocked` не выставлялся НИ РАЗУ. Значит все фильтры
`is_blocked=FALSE` (а на них стоят выборки рассылок и воронок) пропускали
всех подряд, каждая отправка заблокировавшему возвращала 403, и сам продукт
раз за разом стучался туда, откуда его выгнали. Для Telegram это признак
спама, то есть цена — репутация бота, а не лишний запрос.

`database.db.block_user` при этом существовал и не вызывался ниоткуда.

ЧТО ТЕПЕРЬ. Блокировка и возврат отмечаются в базе и подаются сигналом в
виртуальный слой: «заблокировал» — терминальный негатив, возврат — «открыл».
"""
from __future__ import annotations

import inspect

import pytest

from services import auto_responder


class _Pool:
    def __init__(self):
        self.calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))


def _upd(old_status, new_status, chat_type="private", user_id=555):
    return {"chat": {"id": user_id, "type": chat_type},
            "old_chat_member": {"status": old_status},
            "new_chat_member": {"status": new_status}}


class _Signals:
    def __init__(self):
        self.sent: list[tuple] = []

    async def __call__(self, user_id, name, confidence):
        self.sent.append((user_id, name, confidence))


async def test_blocking_is_written_and_signalled():
    pool, sig = _Pool(), _Signals()
    await auto_responder._handle_my_chat_member(pool, 10, _upd("member", "kicked"), sig)
    assert pool.calls, "блокировка не записана — рассылки продолжат бить в 403"
    sql, args = pool.calls[0]
    assert "is_blocked" in sql and args[0] == 10 and args[1] == 555
    assert args[2] is True
    assert sig.sent == [(555, "blocked", 0.95)]


async def test_return_unblocks_and_signals():
    pool, sig = _Pool(), _Signals()
    await auto_responder._handle_my_chat_member(pool, 10, _upd("kicked", "member"), sig)
    assert pool.calls[0][1][2] is False
    assert sig.sent == [(555, "opened", 0.5)]


async def test_group_chats_are_not_subscribers():
    """Тот же статус в группе означает бан бота админом — другой случай."""
    pool, sig = _Pool(), _Signals()
    await auto_responder._handle_my_chat_member(
        pool, 10, _upd("member", "kicked", chat_type="supergroup"), sig)
    assert pool.calls == [] and sig.sent == []


async def test_unchanged_status_does_nothing():
    pool, sig = _Pool(), _Signals()
    await auto_responder._handle_my_chat_member(pool, 10, _upd("member", "member"), sig)
    assert pool.calls == [] and sig.sent == []


async def test_update_without_a_user_is_ignored():
    pool, sig = _Pool(), _Signals()
    await auto_responder._handle_my_chat_member(
        pool, 10, {"chat": {"type": "private"}}, sig)
    assert pool.calls == [] and sig.sent == []


def test_the_update_is_actually_read_from_the_stream():
    src = inspect.getsource(auto_responder)
    assert 'upd.get("my_chat_member")' in src, (
        "обновление снова выбрасывается не глядя")
    i = src.index('upd.get("my_chat_member")')
    assert "_handle_my_chat_member" in src[i:i + 400]


def test_webhook_asks_for_the_update_too():
    """Бот на вебхуке иначе не узнает о блокировке никогда."""
    src = open("services/bot_api.py", encoding="utf-8").read()
    i = src.index("async def set_webhook")
    seg = src[i:src.index("\nasync def ", i + 10)]
    assert '"my_chat_member"' in seg


def test_blocked_signal_is_terminal_in_the_layer():
    from services import virtual_layer as vl
    assert "blocked" in vl.NEGATIVE_SIGNALS
    out = vl.apply_signal({"value": "ready", "confidence": 0.9}, "blocked")
    assert out and out["value"] == vl.LOST
