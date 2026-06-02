"""Система сбора ошибок от пользователей с скриншотами."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PhotoSize
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ErrorReportCb
from bot.states import ErrorReportFSM
from database import db
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

router = Router()


@router.callback_query(ErrorReportCb.filter(F.action == "start"))
async def cb_start_error_report(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало процесса отправки отчёта об ошибке."""
    await callback.answer()
    text = (
        "🐛 <b>Отправить отчёт об ошибке</b>\n\n"
        "Опишите что произошло:\n"
        "• Что именно вы делали\n"
        "• Какая ошибка произошла\n"
        "• Когда это произошло\n\n"
        "<i>Потом я попрошу скриншот для доказательства.</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ErrorReportCb(action="cancel"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await state.set_state(ErrorReportFSM.awaiting_description)


@router.message(ErrorReportFSM.awaiting_description)
async def msg_error_description(message: Message, state: FSMContext) -> None:
    """Получение описания ошибки."""
    if not message.text:
        await message.answer("❌ Пожалуйста отправьте текстовое описание ошибки.")
        return

    description = message.text.strip()
    if len(description) < 10:
        await message.answer("❌ Описание слишком короткое. Опишите подробнее что произошло.")
        return

    await state.update_data(description=description)
    text = (
        "📸 <b>Теперь отправьте скриншот</b>\n\n"
        "Скриншот поможет мне быстро разобраться. "
        "Если ошибка на одном из экранов бота — сделайте снимок этого экрана."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⏩ Пропустить скриншот", callback_data=ErrorReportCb(action="skip_screenshot"))
    kb.button(text="❌ Отмена", callback_data=ErrorReportCb(action="cancel"))
    kb.adjust(1, 1)
    await message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(ErrorReportFSM.awaiting_screenshot)


@router.message(ErrorReportFSM.awaiting_screenshot, F.photo)
async def msg_error_screenshot(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Получение скриншота ошибки."""
    # Берём фото с наибольшим разрешением
    photo: PhotoSize = message.photo[-1]
    file_id = photo.file_id

    sd = await state.get_data()
    description = sd.get("description", "")

    try:
        # Сохраняем в БД
        report_id = await pool.fetchval(
            """INSERT INTO error_reports
               (user_id, description, screenshot_id, status)
               VALUES ($1, $2, $3, $4)
               RETURNING id""",
            message.from_user.id,
            description,
            file_id,
            "new",
        )
        log.info("error_report: saved report_id=%d user_id=%d", report_id, message.from_user.id)

        # Очистим FSM
        await state.clear()

        # Ответ пользователю
        text = (
            f"✅ <b>Спасибо!</b>\n\n"
            f"Ваш отчёт об ошибке успешно отправлен.\n"
            f"ID отчёта: <code>#{report_id}</code>\n\n"
            f"Я проанализирую его и если нужна дополнительная информация — свяжусь с вами."
        )
        await message.answer(text)
    except Exception as e:
        log_exc_swallow(log, f"Ошибка сохранения отчёта об ошибке: {e}")
        await message.answer(
            "❌ Не удалось сохранить отчёт. Попробуйте позже или напишите в поддержку."
        )


@router.message(ErrorReportFSM.awaiting_screenshot)
async def msg_error_screenshot_invalid(message: Message) -> None:
    """Ожидаем именно фото, а не текст."""
    text = (
        "📸 Пожалуйста отправьте <b>фото/скриншот</b> (не текст).\n\n"
        "Можно также пропустить этот шаг через кнопку ниже."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⏩ Пропустить скриншот", callback_data=ErrorReportCb(action="skip_screenshot"))
    kb.button(text="❌ Отмена", callback_data=ErrorReportCb(action="cancel"))
    kb.adjust(1, 1)
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(ErrorReportCb.filter(F.action == "skip_screenshot"))
async def cb_skip_screenshot(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    """Пропустить скриншот и сохранить только описание."""
    await callback.answer()

    sd = await state.get_data()
    description = sd.get("description", "")

    try:
        # Сохраняем без скриншота
        report_id = await pool.fetchval(
            """INSERT INTO error_reports
               (user_id, description, status)
               VALUES ($1, $2, $3)
               RETURNING id""",
            callback.from_user.id,
            description,
            "new",
        )
        log.info("error_report: saved report_id=%d user_id=%d (no screenshot)",
                 report_id, callback.from_user.id)

        # Очистим FSM
        await state.clear()

        # Ответ пользователю
        text = (
            f"✅ <b>Спасибо!</b>\n\n"
            f"Ваш отчёт об ошибке успешно отправлен.\n"
            f"ID отчёта: <code>#{report_id}</code>\n\n"
            f"Я проанализирую его и если нужна дополнительная информация — свяжусь с вами."
        )
        await callback.message.edit_text(text)
    except Exception as e:
        log_exc_swallow(log, f"Ошибка сохранения отчёта об ошибке: {e}")
        await callback.message.edit_text(
            "❌ Не удалось сохранить отчёт. Попробуйте позже или напишите в поддержку."
        )


@router.callback_query(ErrorReportCb.filter(F.action == "cancel"))
async def cb_cancel_error_report(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена отправки отчёта об ошибке."""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Отправка отчёта отменена.")
