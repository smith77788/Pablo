"""Broadcast composer and launcher."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import BroadcastCb, BotCb
from bot.keyboards import broadcast_menu, broadcast_confirm, back_to_bot
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
        "✍️ Напишите текст рассылки.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code>"
    )
    await callback.answer()


@router.message(Broadcast.waiting_message)
async def msg_broadcast_text(message: Message, state: FSMContext,
                              pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    count = await db.get_audience_count(pool, data["bot_id"])
    await state.update_data(text=message.text or message.caption or "")
    await state.set_state(Broadcast.confirming)
    await message.answer(
        f"📢 <b>Предпросмотр:</b>\n\n{message.text}\n\n"
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
    if not text:
        await callback.answer("Текст рассылки пуст.", show_alert=True)
        return

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await state.clear()
        await callback.answer("Бот не найден.", show_alert=True)
        return

    total = await db.get_audience_count(pool, row["bot_id"])
    bc_id = await db.create_broadcast(pool, row["bot_id"], text, total, callback.from_user.id)

    broadcaster.start(pool, http, bc_id, row["token"], row["bot_id"], text)

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

    history = await db.get_recent_broadcasts(pool, row["bot_id"])
    if not history:
        await callback.answer("Рассылок пока не было.", show_alert=True)
        return

    lines = []
    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "cancelled": "❌"}
    for bc in history:
        emoji = status_emoji.get(bc["status"], "❓")
        lines.append(
            f"{emoji} #{bc['id']} | {bc['sent_count']}/{bc['total_users']} "
            f"| {bc['created_at'].strftime('%d.%m %H:%M')}"
        )

    await callback.message.edit_text(
        "📋 <b>Последние рассылки:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=back_to_bot(callback_data.bot_id),
    )
    await callback.answer()
