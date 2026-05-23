"""Audience collection, stats, comparison, and CSV export."""
from __future__ import annotations
import csv
import io
import asyncpg
import aiohttp
from aiogram import Router, F
from aiogram.types import BufferedInputFile, CallbackQuery
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
        f"👥 <b>Аудитория {label}</b>\n\nАктивных пользователей: <b>{count}</b>",
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

    await callback.message.edit_text("⏳ Собираю обновления…")

    updates = await bot_api.fetch_updates(http, row["token"])
    users = bot_api.extract_users_from_updates(updates)
    new_count = await db.upsert_users(pool, row["bot_id"], users)
    total = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    await callback.message.edit_text(
        f"👥 <b>Аудитория {label}</b>\n\n"
        f"Получено апдейтов: {len(updates)}\n"
        f"Новых пользователей: <b>+{new_count}</b>\n"
        f"Всего активных: <b>{total}</b>",
        parse_mode="HTML",
        reply_markup=audience_menu(row["bot_id"]),
    )
    await callback.answer()


@router.callback_query(AudCb.filter(F.action == "stats"))
async def cb_stats(callback: CallbackQuery, callback_data: AudCb,
                    pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    stats = await db.get_audience_stats(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    lang_lines = "\n".join(
        f"  <code>{l['lang']}</code>: {l['count']}" for l in stats["languages"]
    ) or "  нет данных"

    total_all = stats["total"] + stats["inactive"]
    block_pct = round(stats["inactive"] / total_all * 100, 1) if total_all else 0

    text = (
        f"📊 <b>Статистика аудитории {label}</b>\n\n"
        f"👤 Активных: <b>{stats['total']}</b>\n"
        f"🚫 Заблокировали бота: <b>{stats['inactive']}</b> ({block_pct}%)\n"
        f"📌 Всего за всё время: <b>{total_all}</b>\n\n"
        f"📈 <b>Прирост:</b>\n"
        f"  За сутки: <b>+{stats['joined_today']}</b>\n"
        f"  За 7 дней: <b>+{stats['joined_week']}</b>\n"
        f"  За 30 дней: <b>+{stats['joined_month']}</b>\n\n"
        f"🌍 <b>Языки (топ-10):</b>\n{lang_lines}"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=audience_menu(row["bot_id"]))
    await callback.answer()


@router.callback_query(AudCb.filter(F.action == "export"))
async def cb_export(callback: CallbackQuery, callback_data: AudCb,
                     pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    await callback.answer("⏳ Генерирую CSV…")

    rows = await db.get_audience_full(pool, row["bot_id"])
    if not rows:
        await callback.message.answer("📤 Аудитория пуста — нечего экспортировать.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "user_id", "username", "first_name", "last_name",
        "language_code", "first_seen", "last_seen", "is_active",
    ])
    for r in rows:
        writer.writerow([
            r["user_id"],
            r["username"] or "",
            r["first_name"] or "",
            r["last_name"] or "",
            r["language_code"] or "",
            r["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            r["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            r["is_active"],
        ])

    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = row["username"] or str(row["bot_id"])
    filename = f"audience_{safe_label}.csv"
    content = buf.getvalue().encode("utf-8-sig")

    await callback.message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=f"📤 Аудитория <b>{label}</b> — {len(rows)} записей",
        parse_mode="HTML",
    )


@router.callback_query(AudCb.filter(F.action == "scan"))
async def cb_scan(callback: CallbackQuery, callback_data: AudCb,
                   pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    await callback.message.edit_text("⚡ Сканирую все доступные апдейты…")
    await callback.answer()

    from database import db as _db
    from services import bot_api as _api

    start_offset = await _db.get_update_offset(pool, callback_data.bot_id)
    users, last_id = await _api.scan_all_users(http, row["token"], start_offset=start_offset)

    new_count = 0
    if users:
        new_count = await db.upsert_users(pool, row["bot_id"], users)
    if last_id > start_offset:
        await db.set_update_offset(pool, callback_data.bot_id, last_id)

    total = await db.get_audience_count(pool, row["bot_id"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"👥 <b>Аудитория {label}</b>\n\n"
        f"⚡ Просканировано апдейтов до ID #{last_id}\n"
        f"Найдено уникальных пользователей: <b>{len(users)}</b>\n"
        f"Новых добавлено: <b>+{new_count}</b>\n"
        f"Всего активных: <b>{total}</b>",
        parse_mode="HTML",
        reply_markup=audience_menu(row["bot_id"]),
    )


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
        f"   {stats['overlap_pct_a']}% от {label_a}\n"
        f"   {stats['overlap_pct_b']}% от {label_b}",
        parse_mode="HTML",
        reply_markup=back_to_bot(row_a["bot_id"]),
    )
    await callback.answer()
