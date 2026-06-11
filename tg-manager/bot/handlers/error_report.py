"""Система сбора ошибок от пользователей с скриншотами."""

from __future__ import annotations

import logging
import os

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PhotoSize
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ErrorReportCb
from bot.states import ErrorReportFSM
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
        "✏️ <b>Напишите описание в поле сообщения ниже ↓</b>"
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
        await message.answer(
            "❌ Описание слишком короткое. Опишите подробнее что произошло."
        )
        return

    await state.update_data(description=description)
    text = (
        "📸 <b>Теперь сделайте скриншот и отправьте его</b>\n\n"
        "Скриншот <b>обязателен</b> — он помогает быстро найти и исправить проблему.\n\n"
        "✏️ <b>Прикрепите фото в поле сообщения ниже ↓</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ErrorReportCb(action="cancel"))
    await message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(ErrorReportFSM.awaiting_screenshot)


@router.message(ErrorReportFSM.awaiting_screenshot, F.photo)
async def msg_error_screenshot(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Получение скриншота ошибки."""
    photo: PhotoSize = message.photo[-1]
    file_id = photo.file_id

    sd = await state.get_data()
    description = sd.get("description", "")

    try:
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
        log.info(
            "error_report: saved report_id=%d user_id=%d",
            report_id,
            message.from_user.id,
        )

        await state.clear()

        text = (
            f"✅ <b>Спасибо!</b>\n\n"
            f"Ваш отчёт об ошибке успешно отправлен.\n"
            f"ID отчёта: <code>#{report_id}</code>\n\n"
            f"Я проанализирую его и если нужна дополнительная информация — свяжусь с вами."
        )
        await message.answer(text)

        # Notify all admins about the new error report
        user = message.from_user
        user_label = (
            f"@{user.username}" if user.username else user.first_name or str(user.id)
        )
        admin_notify = (
            f"🐛 <b>Новый отчёт об ошибке #{report_id}</b>\n\n"
            f"От: {user_label} (<code>{user.id}</code>)\n"
            f"Описание: {description[:300]}"
        )
        admin_ids_raw = os.getenv("ADMIN_IDS", "")
        admin_ids = [
            int(x.strip()) for x in admin_ids_raw.split(",") if x.strip().isdigit()
        ]
        for admin_id in admin_ids:
            try:
                # Forward screenshot to admin with caption
                await message.bot.send_photo(
                    admin_id,
                    file_id,
                    caption=admin_notify,
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(
                    log,
                    f"error_report: failed to notify admin {admin_id} about report #{report_id}",
                )
    except Exception as e:
        log_exc_swallow(log, f"Ошибка сохранения отчёта об ошибке: {e}")
        await message.answer(
            "❌ Не удалось сохранить отчёт. Попробуйте позже или напишите в поддержку."
        )


@router.message(ErrorReportFSM.awaiting_screenshot)
async def msg_error_screenshot_invalid(message: Message) -> None:
    """Ожидаем именно фото."""
    text = (
        "📸 Пожалуйста отправьте <b>фото/скриншот</b> — это обязательно для анализа ошибки.\n\n"
        "✏️ <b>Прикрепите фото в поле сообщения ниже ↓</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ErrorReportCb(action="cancel"))
    await message.answer(text, reply_markup=kb.as_markup())


@router.callback_query(ErrorReportCb.filter(F.action == "cancel"))
async def cb_cancel_error_report(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена отправки отчёта об ошибке."""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Отправка отчёта отменена.")
