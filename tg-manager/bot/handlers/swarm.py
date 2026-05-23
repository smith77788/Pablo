"""Swarm routing management."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
import asyncpg
from bot.callbacks import SwarmCb, BotCb
from bot.keyboards import swarm_menu, back_to_bot
from database import db

router = Router()

ROLE_LABELS = {
    "entry": "🚪 Entry — точка входа трафика",
    "conversion": "💰 Conversion — конверсионный бот",
    "retention": "🔄 Retention — удержание пользователей",
    "general": "⚙️ General — универсальный",
}

@router.callback_query(SwarmCb.filter(F.action == "menu"))
async def cb_swarm_menu(callback: CallbackQuery, callback_data: SwarmCb,
                         pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    swarm_status = "🟢 Активен в Swarm" if row.get("swarm_enabled") else "⚫ Не в Swarm"
    role = ROLE_LABELS.get(row.get("bot_role", "general"), "⚙️ General")
    cluster = row.get("cluster") or "default"
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role}\n"
        f"Кластер: <code>{cluster}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row),
    )
    await callback.answer()


@router.callback_query(SwarmCb.filter(F.action == "toggle"))
async def cb_swarm_toggle(callback: CallbackQuery, callback_data: SwarmCb,
                           pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    new_state = not row.get("swarm_enabled", False)
    await db.toggle_swarm(pool, callback_data.bot_id, new_state)
    row2 = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row2['username']}" if row2["username"] else row2["first_name"]
    swarm_status = "🟢 Активен в Swarm" if new_state else "⚫ Не в Swarm"
    role = ROLE_LABELS.get(row2.get("bot_role", "general"), "⚙️ General")
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role}\n"
        f"Кластер: <code>{row2.get('cluster') or 'default'}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row2),
    )
    await callback.answer("✅ Статус обновлён")


@router.callback_query(SwarmCb.filter(F.action.startswith("role_")))
async def cb_swarm_role(callback: CallbackQuery, callback_data: SwarmCb,
                         pool: asyncpg.Pool) -> None:
    role = callback_data.action.replace("role_", "")
    if role not in ("entry", "conversion", "retention", "general"):
        await callback.answer("Неверная роль", show_alert=True)
        return
    await db.set_bot_role(pool, callback_data.bot_id, role)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    swarm_status = "🟢 Активен в Swarm" if row and row.get("swarm_enabled") else "⚫ Не в Swarm"
    role_label = ROLE_LABELS.get(role, role)
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role_label}\n"
        f"Кластер: <code>{row.get('cluster') or 'default'}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row),
    )
    await callback.answer(f"✅ Роль: {role_label}")
