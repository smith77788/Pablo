"""Scheduled broadcasts: create, list, cancel."""

from __future__ import annotations
from datetime import datetime
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from bot.callbacks import ScheduleCb, BmCb
from bot.keyboards import schedule_menu, back_to_bot, schedule_template_list
from bot.states import ScheduleBroadcast
from database import db
from aiogram.utils.keyboard import InlineKeyboardBuilder

router = Router()


def _sch_cancel_kb(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ScheduleCb(action="menu", bot_id=bot_id))
    return kb.as_markup()


_DT_HINT = (
    "Введите дату и время в формате:\n"
    "<code>25.12.2025 15:30</code> или <code>25.12 15:30</code>\n\n"
    "Время серверное (UTC)."
)


@router.callback_query(ScheduleCb.filter(F.action == "menu"))
async def cb_schedule_menu(
    callback: CallbackQuery, callback_data: ScheduleCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    schedules = await db.get_bot_schedules(pool, callback_data.bot_id, limit=10)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    status_emoji = {"pending": "⏳", "done": "✅", "cancelled": "❌"}
    lines = []
    for s in schedules:
        emoji = status_emoji.get(s["status"], "❓")
        dt = s["execute_at"].strftime("%d.%m.%Y %H:%M")
        preview = s["message_text"][:35].replace("\n", " ")
        safe_preview = (
            preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        lines.append(f"{emoji} {dt} UTC — {safe_preview}…")

    body = "\n".join(lines) if lines else "Запланированных рассылок пока нет."
    await callback.message.edit_text(
        f"⏰ <b>Расписание — {safe_label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Расписание позволяет запланировать рассылку заранее. Вы пишете текст сейчас, указываете дату и время — и бот сам отправит сообщение всем подписчикам в нужный момент, даже если вы спите.\n\n"
        "💡 <b>Например:</b> запланируйте поздравление с праздником на 00:00, или акцию на пятницу вечером.\n\n"
        f"{body}",
        parse_mode="HTML",
        reply_markup=schedule_menu(callback_data.bot_id, schedules),
    )


@router.callback_query(ScheduleCb.filter(F.action == "create"))
async def cb_schedule_create(
    callback: CallbackQuery, callback_data: ScheduleCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(ScheduleBroadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "⏰ <b>Запланировать рассылку — шаг 1/2</b>\n\n"
        "Напишите текст сообщения.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>",
        parse_mode="HTML",
        reply_markup=_sch_cancel_kb(callback_data.bot_id),
    )


@router.message(ScheduleBroadcast.waiting_message)
async def msg_schedule_message(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    if not text or not text.strip():
        data = await state.get_data()
        await message.answer("❌ Текст не может быть пустым. Попробуйте ещё раз:", reply_markup=_sch_cancel_kb(data.get("bot_id", 0)))
        return
    await state.update_data(text=text)
    await state.set_state(ScheduleBroadcast.waiting_datetime)
    data = await state.get_data()
    await message.answer(
        f"⏰ <b>Шаг 2/2: Дата и время запуска</b>\n\n{_DT_HINT}",
        parse_mode="HTML",
        reply_markup=_sch_cancel_kb(data.get("bot_id", 0)),
    )


@router.message(ScheduleBroadcast.waiting_datetime, F.text)
async def msg_schedule_datetime(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    bot_id = data["bot_id"]
    raw = message.text.strip() if message.text else ""

    execute_at: datetime | None = None
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%d.%m %H:%M":
                dt = dt.replace(year=datetime.utcnow().year)
            execute_at = dt
            break
        except ValueError:
            continue

    if not execute_at:
        await message.answer(
            f"❌ Неверный формат даты.\n\n{_DT_HINT}",
            parse_mode="HTML",
        )
        return

    if execute_at <= datetime.utcnow():
        await message.answer("❌ Время запуска должно быть в будущем. Введите снова:")
        return

    text = data["text"]
    await state.clear()

    schedule_id = await db.create_scheduled(
        pool, bot_id, text, execute_at, message.from_user.id
    )
    await message.answer(
        f"✅ Рассылка запланирована!\n\n"
        f"🕐 Время: <b>{execute_at.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
        f"ID: <code>#{schedule_id}</code>",
        parse_mode="HTML",
        reply_markup=back_to_bot(bot_id),
    )


@router.callback_query(ScheduleCb.filter(F.action == "cancel"))
async def cb_schedule_cancel(
    callback: CallbackQuery, callback_data: ScheduleCb, pool: asyncpg.Pool
) -> None:

    cancelled = await db.cancel_scheduled(
        pool, callback_data.schedule_id, callback.from_user.id
    )
    if not cancelled:
        await callback.answer(
            "❌ Не удалось отменить. Рассылка уже выполнена или не найдена.",
            show_alert=True,
        )
        return
    await callback.answer("✅ Рассылка отменена.")
    schedules = await db.get_bot_schedules(pool, callback_data.bot_id, limit=10)
    await callback.message.edit_reply_markup(
        reply_markup=schedule_menu(callback_data.bot_id, schedules)
    )


@router.callback_query(ScheduleCb.filter(F.action == "from_template"))
async def cb_schedule_from_template(
    callback: CallbackQuery, callback_data: ScheduleCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    templates = await db.get_templates(pool, callback.from_user.id)
    if not templates:
        await callback.answer(
            "Нет шаблонов. Создайте шаблон в разделе шаблонов.", show_alert=True
        )
        return
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Выберите шаблон для планирования:</b>",
        parse_mode="HTML",
        reply_markup=schedule_template_list(callback_data.bot_id, templates),
    )


@router.callback_query(ScheduleCb.filter(F.action == "use_template"))
async def cb_schedule_use_template(
    callback: CallbackQuery,
    callback_data: ScheduleCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:

    # schedule_id repurposed as template_id here
    template = await db.get_template(
        pool, callback_data.schedule_id, callback.from_user.id
    )
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer()
    await state.set_state(ScheduleBroadcast.waiting_datetime)
    await state.update_data(bot_id=callback_data.bot_id, text=template["text"])
    safe_name = (
        template["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    await callback.message.edit_text(
        f"📋 Шаблон: <b>{safe_name}</b>\n\n"
        "Введите дату и время отправки (формат: ДД.ММ.ГГГГ ЧЧ:ММ):",
        parse_mode="HTML",
        reply_markup=_sch_cancel_kb(callback_data.bot_id),
    )
