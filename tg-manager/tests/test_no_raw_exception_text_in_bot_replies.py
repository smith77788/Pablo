"""Текст исключения не уходит в сообщение бота мимо `owner_error`.

Владелец по-английски не читает, а `str(исключения)` Telethon/asyncpg — это
английская фраза, иногда с `<…>`, из-за которой Telegram отвергает
HTML-сообщение целиком. У мини-аппа одна дверь для текста ошибки
(`mini_app_api._err`), у бота её не было: обход 08.11.2026 нашёл 46 мест вида
`answer(f"❌ Ошибка: {e}")`. Теперь дверь есть (`bot/utils/owner_error.py`), и
храповик держит, чтобы новое место не прошло мимо неё.
"""
from __future__ import annotations

import pathlib
import re

from bot.utils.owner_error import owner_error

HANDLERS = pathlib.Path(__file__).resolve().parents[1] / "bot" / "handlers"
_SEND = r"(?:answer|edit_text|reply|send_message|_reply)\("
_RAW = re.compile(_SEND + r"[^\n]*?(?:\{(?:e|exc|err|ex)(?:!s)?\}|str\((?:e|exc|err|ex)\))")


def test_no_handler_sends_raw_exception_text():
    hits = []
    for f in sorted(HANDLERS.glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if _RAW.search(line):
                hits.append(f"{f.name}:{n}: {line.strip()[:120]}")
    assert not hits, "текст исключения мимо owner_error:\n" + "\n".join(hits)


def test_english_exception_becomes_russian_and_escaped():
    out = owner_error(RuntimeError("Chat admin privileges are required to do that <b>"))
    assert "Chat admin" not in out and "журнал" in out
    assert owner_error(ValueError("Неверный формат <x>")) == "Неверный формат &lt;x&gt;"
    assert owner_error(ValueError("Неверный <x>"), html_escape=False) == "Неверный <x>"
    assert owner_error(None)


def test_alert_text_fits_telegram_popup_limit():
    assert len(owner_error(ValueError("Ошибка " * 100), html_escape=False)) <= 150
