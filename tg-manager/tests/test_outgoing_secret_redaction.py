"""Секрет не уходит в сообщение Telegram ни из одного места бота.

В боте 47 мест вида `await message.answer(f"⚠️ Ошибка: {e}")`. Обычно это
безвредно, но текст исключения бывает с секретом: ошибка запроса через прокси
несёт `socks5://логин:пароль@хост`, ошибка asyncpg — строку подключения с
паролем, ошибка Bot API — токен в URL. Сообщение в Telegram удалить оттуда
нельзя, поэтому чистка стоит на выходе, а не в 47 местах по одному.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram import Bot

from bot.utils.outgoing_redaction import install_outgoing_secret_redaction


@pytest.fixture(autouse=True)
def _patched():
    install_outgoing_secret_redaction()
    yield


class _Spy(Bot):
    """Бот, который ничего не отправляет, а запоминает, что ему передали."""

    def __init__(self):
        # Настоящий __init__ требует валидный токен и поднимает сессию —
        # обходим его: нам нужны только унаследованные (уже пропатченные)
        # методы и запись вызова.
        self.calls: list[tuple] = []

    async def __call__(self, method, *a, **kw):   # aiogram зовёт это в конце
        self.calls.append(method)
        return method


def _sent_text(bot):
    assert bot.calls, "метод не доехал до отправки"
    m = bot.calls[-1]
    return getattr(m, "text", None) or getattr(m, "caption", None)


TOKEN = "1234567890:AAHabcdefghijklmnopqrstuvwxyz012345"
PROXY = "socks5://user:pa55word@10.1.2.3:1080"
SESSION = "1Ab" + "Xy9" * 40


def test_bot_token_in_a_message_is_masked():
    bot = _Spy()
    asyncio.run(bot.send_message(555, f"⚠️ Ошибка: {TOKEN}"))
    out = _sent_text(bot)
    assert "1234567890:***" in out
    assert "AAHabc" not in out


def test_proxy_password_in_a_message_is_masked():
    bot = _Spy()
    asyncio.run(bot.send_message(555, f"⚠️ Прокси не отвечает: {PROXY}"))
    out = _sent_text(bot)
    assert "socks5://user:***@10.1.2.3:1080" in out
    assert "pa55word" not in out


def test_session_string_in_a_message_is_masked():
    bot = _Spy()
    asyncio.run(bot.send_message(555, f"сессия {SESSION} битая"))
    assert SESSION not in _sent_text(bot)


def test_text_passed_by_keyword_is_cleaned_too():
    bot = _Spy()
    asyncio.run(bot.send_message(chat_id=555, text=f"вот {PROXY}"))
    assert "pa55word" not in _sent_text(bot)


def test_caption_is_cleaned():
    bot = _Spy()
    asyncio.run(bot.send_photo(555, "file_id", caption=f"отчёт {TOKEN}"))
    out = _sent_text(bot)
    assert "1234567890:***" in out


def test_callback_alert_is_cleaned():
    bot = _Spy()
    asyncio.run(bot.answer_callback_query("cb1", text=f"⚠️ {PROXY}", show_alert=True))
    assert "pa55word" not in _sent_text(bot)


def test_ordinary_text_is_untouched_and_not_truncated():
    """Чистка не должна ни менять обычный текст, ни обрезать длинный.

    У redact_secrets потолок по умолчанию 2000 символов; без явной длины
    длинный пост уезжал бы обрезанным — это было бы хуже утечки, потому что
    ломалось бы молча и постоянно.
    """
    bot = _Spy()
    long_text = "Отчёт по операции. " * 300      # ~5700 символов
    asyncio.run(bot.send_message(555, long_text))
    assert _sent_text(bot) == long_text


def test_installing_twice_does_not_double_wrap():
    install_outgoing_secret_redaction()
    install_outgoing_secret_redaction()
    bot = _Spy()
    asyncio.run(bot.send_message(555, f"вот {TOKEN}"))
    out = _sent_text(bot)
    assert out.count("***") == 1, out


def test_main_installs_it_before_handlers():
    """Патч обязан ставиться на старте: иначе часть сообщений уйдёт нечищеной."""
    src = open("main.py", encoding="utf-8").read()
    assert "install_outgoing_secret_redaction()" in src
    assert src.index("install_outgoing_secret_redaction()") < src.index(
        "from bot.handlers import start"), (
        "патч ставится ПОСЛЕ импорта обработчиков — часть путей останется "
        "непокрытой")
