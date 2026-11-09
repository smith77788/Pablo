"""Накопленное за перезапуск обрабатывается; отсекается только давнее личное.

ЧТО БЫЛО. В режиме вебхука бот ставил его с `drop_pending_updates=True`:
Telegram копит апдейты, пока бот не забирает их, и этот флаг просил выбросить
всё накопленное. Деплой едет на каждый принятый PR, поэтому всё, что люди
писали и нажимали в минуту перезапуска, пропадало без ответа. (В режиме
поллинга тот же аргумент ничего не делал: у `start_polling` в aiogram 3 его
нет, он молча уходил в данные обработчиков.)

ЧТО ТЕПЕРЬ. Накопленное обрабатывается в обоих режимах. Отбрасывается только
ЛИЧНОЕ сообщение старше `STALE_UPDATE_MAX_AGE_SEC` (по умолчанию 15 минут):
после долгой аварии отвечать на вчерашнее «/start» хуже, чем промолчать.
Сообщения в группах не трогаем: модератор чатов обязан увидеть и удалить спам,
пришедший, пока бот лежал. У нажатия кнопки (callback_query) даты нет, поэтому
оно идёт дальше всегда.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from services import metrics

log = logging.getLogger(__name__)

SKIPPED = "infragram_bot_stale_updates_skipped_total"

_DEFAULT_MAX_AGE = 900.0
# Меньше минуты не принимаем: опечатка в переменной не должна снова начать
# выбрасывать сообщения, отправленные за секунды до перезапуска.
_MIN_MAX_AGE = 60.0


def max_age_sec() -> float:
    raw = (os.getenv("STALE_UPDATE_MAX_AGE_SEC") or "").strip()
    if not raw:
        return _DEFAULT_MAX_AGE
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_MAX_AGE
    return max(value, _MIN_MAX_AGE)


def message_age_sec(message: Message, now: float | None = None) -> float | None:
    """Возраст сообщения в секундах или None, если даты нет."""
    when = getattr(message, "date", None)
    if when is None:
        return None
    if isinstance(when, datetime):
        ts = when.timestamp()
    else:
        try:
            ts = float(when)
        except (TypeError, ValueError):
            return None
    return (time.time() if now is None else now) - ts


class StaleUpdateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and getattr(event.chat, "type", None) == "private":
            age = message_age_sec(event)
            if age is not None and age > max_age_sec():
                metrics.inc(SKIPPED)
                log.info("Пропускаю сообщение возрастом %d с (накопилось за простой)", int(age))
                return None
        return await handler(event, data)
