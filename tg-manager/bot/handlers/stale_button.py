"""Последняя сетка для нажатий, которые никто не разобрал.

Кнопка в старом сообщении бота живёт вечно, а её обработчик — нет: многие
меню привязаны к шагу диалога (FSM), и после /cancel, другой команды или
истечения шага такая кнопка не находит владельца. Telegram в этом случае
крутит «часики» на кнопке до таймаута, а человек не получает ничего — для
него это «мёртвая кнопка». Обход всех кнопок бота 08.11.2026 нашёл пять таких
на одном только стартовом дереве (меню передачи подарков, шаблоны ботов).

Роутер подключается ПОСЛЕДНИМ в `main.build_dispatcher`, поэтому сюда
доходят только нажатия, которые не взял ни один другой обработчик. Ответ —
короткое объяснение и путь обратно, а не тишина.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.types import CallbackQuery

log = logging.getLogger(__name__)
router = Router(name="stale_button")

STALE_TEXT = ("⌛ Это меню устарело — кнопка больше не действует.\n\n"
              "Откройте раздел заново: /menu")


@router.callback_query()
async def cb_stale_button(callback: CallbackQuery) -> None:
    log.info("неразобранное нажатие: %r", (callback.data or "")[:64])
    try:
        await callback.answer(STALE_TEXT, show_alert=True)
    except Exception:
        log.debug("cb_stale_button: ответ не ушёл", exc_info=True)
