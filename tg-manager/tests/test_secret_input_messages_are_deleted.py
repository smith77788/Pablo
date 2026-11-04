"""Сообщения с паролями 2FA, кодами, сессиями и токенами не остаются в чате.

Обработчики читали секрет и оставляли сообщение в истории Telegram (вечной и
синхронизированной на все устройства). Строка сессии или пароль 2FA в истории
— полный доступ к аккаунту. Тест падает без фикса.
"""
from __future__ import annotations

import asyncio
import pathlib

from aiogram.fsm.state import State

from bot.middlewares.secret_input import SECRET_STATES, SecretInputMiddleware


def _all_states() -> set[str]:
    from bot import states as s1
    from bot.handlers import accounts as s2
    out = set()
    for mod in (s1, s2):
        for obj in vars(mod).values():
            if isinstance(obj, type):
                for v in vars(obj).values():
                    if isinstance(v, State) and v.state:
                        out.add(v.state)
    return out


def test_every_listed_state_exists():
    missing = SECRET_STATES - _all_states()
    assert not missing, f"переименованы состояния — обновите SECRET_STATES: {missing}"


class _FSM:
    def __init__(self, st):
        self.st = st

    async def get_state(self):
        return self.st


class _Msg:
    def __init__(self, chat_type="private"):
        from types import SimpleNamespace
        self.chat = SimpleNamespace(type=chat_type)
        self.deleted = False

    async def delete(self):
        self.deleted = True


def _run(state, chat_type="private"):
    import bot.middlewares.secret_input as mod
    msg = _Msg(chat_type)
    orig = mod.Message
    mod.Message = _Msg  # isinstance по подставному классу
    try:
        seen = []

        async def handler(ev, data):
            seen.append(ev.deleted)   # обработчик видит сообщение до удаления
            return "ok"

        asyncio.run(SecretInputMiddleware()(handler, msg, {"state": _FSM(state)}))
    finally:
        mod.Message = orig
    return msg.deleted, seen


def test_secret_state_message_is_deleted_after_handler():
    deleted, seen = _run("AccountLogin:waiting_2fa")
    assert deleted and seen == [False]


def test_ordinary_messages_stay():
    assert _run(None)[0] is False
    assert _run("AddAutoReply:waiting_keyword")[0] is False
    assert _run("AccountLogin:waiting_2fa", "supergroup")[0] is False


def test_registered_in_main():
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "dp.message.outer_middleware(SecretInputMiddleware())" in src
