"""Второй ответ на ту же кнопку не теряется молча.

Telegram принимает ответ на нажатие (answerCallbackQuery) РОВНО ОДИН раз.
Частый рисунок в обработчиках: сразу `safe_answer(callback)` (снять «часики»),
а ниже, в ветке ошибки или пустоты, — `callback.answer("Нет аккаунтов",
show_alert=True)`. Второй ответ Telegram отвергает, и человек не видит ни
предупреждения, ни ошибки: кнопка просто «ничего не сделала». Обход всех
кнопок бота 08.11.2026 нашёл так 12 нажатий (передача подарков, запуск
самопиара, все девять тумблеров уведомлений).

Править каждое место по одному бесполезно — завтра напишут следующее. Здесь
обёртка на `CallbackQuery.answer`: первый ответ уходит как обычно, а
повторный с текстом превращается в сообщение в тот же чат, то есть доходит.
Повторный без текста ничего не делает (снимать «часики» уже нечего).

Память о нажатиях — в одном процессе и ограничена по размеру: нажатие целиком
обрабатывает один процесс, делить её между репликами нечего.
"""
from __future__ import annotations

import logging
from collections import OrderedDict

from aiogram.types import CallbackQuery

log = logging.getLogger(__name__)

_MAX_REMEMBERED = 5000
_answered: "OrderedDict[str, bool]" = OrderedDict()
_ORIGINAL = CallbackQuery.answer


async def _noop() -> bool:
    return True


def _answer(self: CallbackQuery, text: str | None = None, show_alert: bool | None = None,
            url: str | None = None, cache_time: int | None = None, **kwargs):
    if self.id in _answered:
        if text and self.message is not None and self.bot is not None:
            log.info("повторный ответ на нажатие ушёл сообщением: %r", text[:60])
            return self.bot.send_message(self.message.chat.id, text, parse_mode=None)
        return _noop()
    _answered[self.id] = True
    while len(_answered) > _MAX_REMEMBERED:
        _answered.popitem(last=False)
    return _ORIGINAL(self, text=text, show_alert=show_alert, url=url,
                     cache_time=cache_time, **kwargs)


def install() -> None:
    """Идемпотентно: повторный вызов ничего не меняет."""
    if CallbackQuery.answer is not _answer:
        CallbackQuery.answer = _answer
