"""Второй ответ на ту же кнопку доходит до человека.

Telegram принимает answerCallbackQuery один раз. Обработчики часто снимают
«часики» сразу, а предупреждение («Нет аккаунтов», «Ошибка») шлют вторым
ответом ниже — и его Telegram отвергает. Обход кнопок бота 08.11.2026 нашёл
12 таких нажатий. Обёртка `bot/utils/callback_answer_once` превращает
повторный ответ с текстом в сообщение в тот же чат.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.types import CallbackQuery, Chat, Message, User

from bot.utils import callback_answer_once


def _cb(cid: str) -> CallbackQuery:
    user = User(id=7, is_bot=False, first_name="x")
    msg = Message(message_id=1, date=0, chat=Chat(id=7, type="private"), text="x")
    return CallbackQuery(id=cid, from_user=user, chat_instance="c", message=msg, data="d")


def test_second_answer_with_text_becomes_a_message(monkeypatch):
    callback_answer_once.install()
    sent = []
    monkeypatch.setattr(callback_answer_once, "_ORIGINAL",
                        lambda self, **kw: sent.append(("answer", kw.get("text"))) or _done())
    bot = MagicMock()
    bot.send_message = AsyncMock()
    cb = _cb("q-second-answer").as_(bot)

    async def go():
        await cb.answer()                               # снять «часики»
        await cb.answer("Нет аккаунтов", show_alert=True)  # раньше терялось
        await cb.answer()                               # пустой повтор — ничего

    asyncio.run(go())
    assert sent == [("answer", None)]
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args == (7, "Нет аккаунтов")


def test_install_is_idempotent():
    callback_answer_once.install()
    callback_answer_once.install()
    assert CallbackQuery.answer is callback_answer_once._answer


async def _done():
    return True
