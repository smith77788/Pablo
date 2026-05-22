"""Audience collection, stats, and comparison."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
import aiohttp
import asyncpg
from bot.callbacks import AudCb, BotCb
from bot.keyboards import audience_menu, bots_pick, back_to_bot
from database import db
from services import bot_api

router = Router()


@router.callback_query(AudCb.filter(F.action == "menu"))
async def cb_aud_menu(callback: CallbackQuery, callback_data: AudCb,
                       pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    count = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"👥 <b>Аудитория {label}</b>\n\nСобрано: <b>{count}</b> пользователей",
        parse_mode="HTML",
        reply_markup=audience_menu(row["bot_id"]),
    )
    await callback.answer()


@router.callback_query(AudCb.filter(F.action == "refresh"))
async def cb_refresh(callback: CallbackQuery, callback_data: AudCb,
                      pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Собираю обновления...")

    updates = await bot_api.fetch_updates(http, row["token"])
    users = bot_api.extract_users_from_updates(updates)
    new_count = await db.upsert_users(pool, row["bot_id"], users)
    total = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    await callback.message.edit_text(
        f"👥 <b>Аудитория {label}</b>\n\n"
        f"Получено из апдейтов: {len(updates)}\n"
        f"Новых пользователей: <b>+{new_count}</b>\n"
        f"Всего: <b>{total}</b>",
        parse_mode="HTML",
        reply_markup=audience_menu(row["bot_id"]),
    )
    await callback.answer()


@router.callback_query(AudCb.filter(F.action == "compare"))
async def cb_compare_pick(callback: CallbackQuery, callback_data: AudCb,
                           pool: asyncpg.Pool) -> None:
    bots = await db.get_bots(pool, callback.from_user.id)
    others = [b for b in bots if b["bot_id"] != callback_data.bot_id]
    if not others:
        await callback.answer(
            "Нужен хотя бы ещё один бот для сравнения.", show_alert=True
        )
        return
    await callback.message.edit_text(
        "⚖️ Выберите второй бот для сравнения аудиторий:",
        reply_markup=bots_pick(bots, exclude_bot_id=callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(AudCb.filter(F.action == "pick_b"))
async def cb_compare_result(callback: CallbackQuery, callback_data: AudCb,
                              pool: asyncpg.Pool) -> None:
    row_a = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    row_b = await db.get_bot(pool, callback_data.target_id, callback.from_user.id)
    if not row_a or not row_b:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    stats = await db.compare_audiences(pool, row_a["bot_id"], row_b["bot_id"])
    label_a = f"@{row_a['username']}" if row_a["username"] else row_a["first_name"]
    label_b = f"@{row_b['username']}" if row_b["username"] else row_b["first_name"]

    await callback.message.edit_text(
        f"⚖️ <b>Сравнение аудиторий</b>\n\n"
        f"<b>{label_a}</b>: {stats['count_a']} чел.\n"
        f"<b>{label_b}</b>: {stats['count_b']} чел.\n\n"
        f"🔁 Пересечение: <b>{stats['overlap']}</b> чел.\n"
        f"   ({stats['overlap_pct_a']}% от {label_a}, "
        f"{stats['overlap_pct_b']}% от {label_b})",
        parse_mode="HTML",
        reply_markup=back_to_bot(row_a["bot_id"]),
    )
    await callback.answer()
