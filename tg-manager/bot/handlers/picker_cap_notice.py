"""Объяснение к срезанному списку выбора — одно на все экраны.

Экраны выбора отдают не больше `picker_cap.DEFAULT_LIMIT` кнопок (почему —
в docstring `bot/utils/picker_cap.py`). Последней кнопкой они ставят подпись
об остатке; нажатие на неё попадает сюда и получает объяснение, а не молчание.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery

from bot.callbacks import CapCb

router = Router()

_WHAT = {
    "acc": "аккаунтов",
    "ch": "каналов",
    "bot": "ботов",
    "grp": "групп",
}


@router.callback_query(CapCb.filter())
async def cb_cap_notice(callback: CallbackQuery, callback_data: CapCb) -> None:
    what = _WHAT.get(callback_data.what, "объектов")
    hidden = max(0, callback_data.total - callback_data.shown)
    await callback.answer(
        f"Показаны первые {callback_data.shown} из {callback_data.total} {what}.\n\n"
        f"Ещё {hidden} не поместились: Telegram ограничивает размер клавиатуры. "
        f"Полный список с поиском и страницами — в мини-аппе.",
        show_alert=True,
    )
