"""Экран длиннее 4096 знаков обрезается, а не пропадает.

Telegram отвергает сообщение длиннее 4096 знаков целиком («message is too
long»). Экраны бота собираются из данных — список аккаунтов, целей, журнал, —
и на большом флоте нажатие просто ничего не показывало. Защита на сессии бота
(`bot/utils/long_text_guard`) режет такой текст по строкам, закрывает теги и
пишет, что дальше не поместилось. Падает без фикса.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from aiogram.methods import EditMessageText, SendMessage

from bot.utils import long_text_guard as g
from services.error_codes import ErrorCode, from_exception

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(method):
    seen = []

    async def make_request(bot, m):
        seen.append(m)
        return True

    asyncio.run(g.LongTextGuard()(make_request, None, method))
    return seen[0]


def _balanced(text: str) -> bool:
    stack = []
    for closing, name in re.findall(r"<(/?)([a-z]+)[^>]*>", text):
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def test_long_html_screen_is_cut_by_lines_with_tags_closed() -> None:
    rows = "\n".join(f"• <b>Аккаунт #{i}</b> — <code>+7999{i:07d}</code> &amp; ок" for i in range(400))
    text = f"<b>📱 Аккаунты</b>\n<blockquote>\n{rows}\n</blockquote>"
    assert g.visible_len(text, as_html=True) > g.TG_LIMIT
    sent = _run(SendMessage(chat_id=1, text=text, parse_mode="HTML"))
    assert g.visible_len(sent.text, as_html=True) <= g.TG_LIMIT
    assert sent.text.endswith(g.TAIL)
    body = sent.text[: -len(g.TAIL)]
    assert _balanced(body), body[-200:]
    assert not re.search(r"&(?!(amp|lt|gt|quot|#x27);)", body)


def test_edit_is_cut_too_and_short_text_is_untouched() -> None:
    long_plain = "строка\n" * 1000
    edited = _run(EditMessageText(chat_id=1, message_id=2, text=long_plain, parse_mode=None))
    assert len(edited.text) <= g.TG_LIMIT and edited.text.endswith(g.TAIL)
    short = SendMessage(chat_id=1, text="✅ Готово", parse_mode="HTML")
    assert _run(short) is short


def test_one_huge_line_is_cut_as_plain_text() -> None:
    text = "<b>" + "x&amp;" * 3000 + "</b>"
    sent = _run(SendMessage(chat_id=1, text=text, parse_mode="HTML"))
    assert g.visible_len(sent.text, as_html=True) <= g.TG_LIMIT
    assert "<b>" not in sent.text


def test_guard_is_installed_on_the_real_bot() -> None:
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "long_text_guard.install(bot)" in src


def test_telegram_wording_is_classified() -> None:
    exc = Exception("Telegram server says - Bad Request: message is too long")
    assert from_exception(exc) == ErrorCode.TG_MESSAGE_TOO_LONG


def test_install_is_idempotent() -> None:
    from aiogram import Bot

    bot = Bot(token="123456:" + "A" * 35)
    g.install(bot)
    g.install(bot)
    assert sum(isinstance(m, g.LongTextGuard) for m in bot.session.middleware) == 1
