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

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.utils.op_helpers import cancel_kb

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


WRONG_STEP_TEXT = (
    "✋ Сейчас бот ждёт ответа на предыдущий шаг — обычно это выбор кнопкой "
    "в сообщении выше.\n\n"
    "Ответьте на него или нажмите «✖️ Отменить», чтобы выйти."
)


@router.message(F.chat.type == "private")
async def msg_wrong_step(message: Message, state: FSMContext) -> None:
    """Текст посреди шага, который ждёт нажатия (выбор аккаунта, тип,
    подтверждение), не брал никто: в таком шаге 87 состояний диалога, и
    человек, написавший ответ словами, не получал ничего. Сюда доходит только
    то, что не разобрал ни один обработчик. Без активного шага молчим, как и
    раньше: там это обычный текст, а не ответ на вопрос."""
    if await state.get_state() is None:
        return
    try:
        await message.answer(WRONG_STEP_TEXT, reply_markup=cancel_kb())
    except Exception:
        log.debug("msg_wrong_step: ответ не ушёл", exc_info=True)
