"""Нажатие, которое не разобрал ни один обработчик, получает ответ.

Без последней сетки (bot/handlers/stale_button.py) кнопка из старого
сообщения или меню, привязанного к шагу диалога, оставляла «часики» до
таймаута Telegram и ничего не говорила — обход кнопок бота 08.11.2026 нашёл
пять таких на стартовом дереве. Проверяем и сам ответ, и то, что роутер
подключён последним: стоящий раньше, он перехватил бы чужие кнопки.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock

from bot.handlers import stale_button

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def test_unhandled_press_is_answered_with_a_way_back():
    cb = MagicMock()
    cb.data = "gt:recipients"
    cb.answer = AsyncMock()
    asyncio.run(stale_button.cb_stale_button(cb))
    cb.answer.assert_awaited_once()
    text = cb.answer.await_args.args[0]
    assert "/menu" in text and cb.answer.await_args.kwargs.get("show_alert") is True


def test_stale_router_is_included_last():
    src = MAIN.read_text(encoding="utf-8")
    body = src[src.index("def build_dispatcher"):]
    body = body[:body.index("return dp")]
    includes = re.findall(r"dp\.include_router\((\w+)\.router\)", body)
    assert includes[-1] == "stale_button_handler", includes[-3:]


def _msg():
    m = MagicMock()
    m.answer = AsyncMock()
    return m


def test_text_in_a_button_step_gets_an_answer_with_a_way_out():
    """87 шагов диалога ждут нажатия (выбор аккаунта, тип, подтверждение) и не
    принимают текст: написавший ответ словами раньше не получал ничего."""
    st = MagicMock()
    st.get_state = AsyncMock(return_value="BulkCreateFSM:choosing_type")
    m = _msg()
    asyncio.run(stale_button.msg_wrong_step(m, st))
    m.answer.assert_awaited_once()
    assert "предыдущий шаг" in m.answer.await_args.args[0]
    kb = m.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].text == "✖️ Отменить"


def test_text_without_an_active_step_stays_silent():
    st = MagicMock()
    st.get_state = AsyncMock(return_value=None)
    m = _msg()
    asyncio.run(stale_button.msg_wrong_step(m, st))
    m.answer.assert_not_awaited()


def test_wrong_step_answer_is_private_chats_only():
    src = pathlib.Path(stale_button.__file__).read_text(encoding="utf-8")
    assert '@router.message(F.chat.type == "private")' in src
