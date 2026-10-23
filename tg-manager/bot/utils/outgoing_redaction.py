"""Секрет не уходит в сообщение Telegram ни из одного места бота.

В боте 47 мест, где текст исключения попадает прямо в сообщение пользователю:
`await message.answer(f"⚠️ Ошибка: {e}")`. Это удобно и обычно безвредно, но
некоторые исключения несут секрет в своём тексте:

* ошибка запроса через прокси (aiohttp/Telethon) — сам URL прокси, а он
  `socks5://логин:пароль@хост:порт`;
* ошибка подключения к базе (asyncpg) — строку подключения с паролем;
* ошибка Telegram Bot API — иногда токен бота в URL запроса.

Сообщение в Telegram — это чужая, неподконтрольная нам история: удалить оттуда
секрет мы не можем. Править 47 мест по одному бессмысленно — 48-е напишут
завтра. Поэтому чистка стоит на ВЫХОДЕ: обёртка на методах aiogram, которые
отправляют текст. У API мини-аппа тот же приём — единственная дверь ответа
(`_json_resp`, `_err`).

Что НЕ маскируется: см. `services/secret_masking.redact_secrets`. Токен бота
превращается в «<id>:***» (id оставляем — по нему бота ищут), строка сессии и
пароль в URL — в «***».
"""
from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from aiogram import Bot

from services.secret_masking import redact_secrets

_PATCHED_ATTR = "_botmother_outgoing_redaction_patched"

# Методы, у которых есть текст, видимый человеком.
_TEXT_METHODS = (
    "send_message",
    "edit_message_text",
    "answer_callback_query",
    "send_photo",
    "send_document",
    "send_video",
    "send_animation",
    "edit_message_caption",
)

# Имена аргументов с человекочитаемым текстом.
_TEXT_ARGS = ("text", "caption")


def _clean(value: Any) -> Any:
    """Вычистить секреты из строки, НЕ обрезая её.

    limit=len(value) обязателен: у redact_secrets потолок по умолчанию 2000
    символов, и без этого длинный пост молча уезжал бы обрезанным.
    """
    if not isinstance(value, str) or not value:
        return value
    return redact_secrets(value, limit=len(value))


def _wrap(method: Callable) -> Callable:
    """Обёртка, чистящая текстовые аргументы — и по имени, и по позиции."""
    positions: dict[str, int] = {}
    try:
        params = list(inspect.signature(method).parameters)
        for name in _TEXT_ARGS:
            if name in params:
                positions[name] = params.index(name)   # включая self
    except (TypeError, ValueError):       # pragma: no cover — на всякий случай
        positions = {}

    @functools.wraps(method)
    async def wrapped(*args: Any, **kwargs: Any):
        for name in _TEXT_ARGS:
            if name in kwargs:
                kwargs[name] = _clean(kwargs[name])
                continue
            idx = positions.get(name)
            if idx is not None and idx < len(args) and isinstance(args[idx], str):
                args = args[:idx] + (_clean(args[idx]),) + args[idx + 1:]
        return await method(*args, **kwargs)

    return wrapped


def install_outgoing_secret_redaction() -> None:
    """Поставить чистку один раз на процесс. Повторный вызов — ничего не делает."""
    if getattr(Bot, _PATCHED_ATTR, False):
        return
    for name in _TEXT_METHODS:
        original = getattr(Bot, name, None)
        if original is None or not callable(original):
            continue
        setattr(Bot, name, _wrap(original))
    setattr(Bot, _PATCHED_ATTR, True)
