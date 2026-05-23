"""Broadcast composer and launcher."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import BroadcastCb, BotCb
from bot.keyboards import (
    broadcast_menu, broadcast_confirm, back_to_bot, broadcast_from_template,
    broadcast_history, broadcast_detail,
)
from bot.states import Broadcast
from database import db
from services import broadcaster

router = Router()


@router.callback_query(BroadcastCb.filter(F.action == "menu"))
async def cb_bc_menu(callback: CallbackQuery, callback_data: BroadcastCb,
                      pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    count = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"📢 <b>Рассылка {label}</b>\n\nАктивная аудитория: <b>{count}</b> чел.",
        parse_mode="HTML",
        reply_markup=broadcast_menu(callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "compose"))
async def cb_compose(callback: CallbackQuery, callback_data: BroadcastCb,
                      state: FSMContext) -> None:
    await state.set_state(Broadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "✍️ Напишите текст рассылки или отправьте фото с подписью.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>"
    )
    await callback.answer()


@router.message(Broadcast.waiting_message)
async def msg_broadcast_text(message: Message, state: FSMContext,
                              pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    count = await db.get_audience_count(pool, data["bot_id"])

    if message.photo:
        photo_file_id = message.photo[-1].file_id
        text = message.caption or ""
    elif message.text:
        photo_file_id = None
        text = message.text
    else:
        await message.answer("❌ Отправьте текст или фото с подписью.")
        return

    await state.update_data(text=text, photo_file_id=photo_file_id)
    await state.set_state(Broadcast.confirming)

    if photo_file_id:
        preview_header = "📸 <b>Фото + подпись:</b>\n\n"
    else:
        preview_header = "📢 <b>Предпросмотр:</b>\n\n"

    await message.answer(
        f"{preview_header}{text}\n\n"
        f"Получателей: <b>{count}</b> чел.\nЗапустить?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(data["bot_id"]),
    )


@router.callback_query(BroadcastCb.filter(F.action == "confirm"))
async def cb_confirm(callback: CallbackQuery, callback_data: BroadcastCb,
                      state: FSMContext, pool: asyncpg.Pool,
                      http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    text = data.get("text", "")
    photo_file_id = data.get("photo_file_id")
    if not text and not photo_file_id:
        await callback.answer("Текст рассылки пуст.", show_alert=True)
        return

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await state.clear()
        await callback.answer("Бот не найден.", show_alert=True)
        return

    total = await db.get_audience_count(pool, row["bot_id"])
    bc_id = await db.create_broadcast(pool, row["bot_id"], text, total, callback.from_user.id, photo_file_id)

    broadcaster.start(pool, http, bc_id, row["token"], row["bot_id"], text, photo_file_id)

    await state.clear()
    await callback.message.edit_text(
        f"🚀 Рассылка #{bc_id} запущена!\n"
        f"Получателей: {total}\n\n"
        "Проверить статус можно в меню «📋 История».",
        reply_markup=back_to_bot(callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    bot_id = data.get("bot_id", 0)
    await callback.message.edit_text(
        "❌ Рассылка отменена.",
        reply_markup=back_to_bot(bot_id) if bot_id else None,
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "status"))
async def cb_status(callback: CallbackQuery, callback_data: BroadcastCb,
                    pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    history = await db.get_recent_broadcasts(pool, row["bot_id"], limit=10)
    if not history:
        await callback.answer("Рассылок пока не было.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"📋 <b>История рассылок — {label}</b>\n\nНажмите на рассылку для деталей:",
        parse_mode="HTML",
        reply_markup=broadcast_history(callback_data.bot_id, history),
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "detail"))
async def cb_detail(callback: CallbackQuery, callback_data: BroadcastCb,
                    pool: asyncpg.Pool) -> None:
    bc = await db.get_broadcast(pool, callback_data.broadcast_id)
    if not bc:
        await callback.answer("Рассылка не найдена.", show_alert=True)
        return
    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    emoji = status_emoji.get(bc["status"], "❓")
    preview = bc["message_text"][:300] if bc["message_text"] else ""
    success_rate = round(bc["sent_count"] / bc["total_users"] * 100) if bc["total_users"] else 0
    finished = bc["finished_at"].strftime("%d.%m.%Y %H:%M") if bc.get("finished_at") else "—"
    text = (
        f"📋 <b>Рассылка #{bc['id']}</b>\n\n"
        f"Статус: {emoji} {bc['status']}\n"
        f"Создана: {bc['created_at'].strftime('%d.%m.%Y %H:%M')}\n"
        f"Завершена: {finished}\n\n"
        f"Отправлено: <b>{bc['sent_count']}</b> / {bc['total_users']} ({success_rate}%)\n"
        f"Ошибок: {bc['failed_count']}\n\n"
        f"<b>Текст:</b>\n{preview}"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=broadcast_detail(callback_data.bot_id))
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "from_template"))
async def cb_from_template(callback: CallbackQuery, callback_data: BroadcastCb,
                            pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    templates = await db.get_templates(pool, callback.from_user.id)
    if not templates:
        await callback.answer("У вас нет шаблонов. Создайте шаблон в разделе шаблонов.", show_alert=True)
        return
    await callback.message.edit_text(
        "📋 <b>Выберите шаблон для рассылки:</b>",
        parse_mode="HTML",
        reply_markup=broadcast_from_template(callback_data.bot_id, templates),
    )
    await callback.answer()


@router.callback_query(BroadcastCb.filter(F.action == "use_template"))
async def cb_use_template(callback: CallbackQuery, callback_data: BroadcastCb,
                           pool: asyncpg.Pool, state: FSMContext) -> None:
    # broadcast_id field repurposed here as template_id
    template = await db.get_template(pool, callback_data.broadcast_id, callback.from_user.id)
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    total = await db.get_audience_count(pool, callback_data.bot_id)
    if total == 0:
        await callback.answer("У бота нет аудитории для рассылки.", show_alert=True)
        return

    await state.set_state(Broadcast.confirming)
    await state.update_data(bot_id=callback_data.bot_id, text=template["text"])

    preview = template["text"][:200]
    await callback.message.edit_text(
        f"📋 <b>Шаблон: {template['name']}</b>\n\n"
        f"Превью:\n{preview}\n\n"
        f"👥 Получателей: <b>{total}</b>\n\n"
        "Отправить эту рассылку?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(callback_data.bot_id),
    )
    await callback.answer()
