"""Hermes Relay inbox handler — manage relay, route operator replies back to users."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import RelayCb, BotCb
from bot.keyboards import relay_menu, back_to_bot
from database import db
from services import bot_api

router = Router()


async def _relay_menu_text(row: asyncpg.Record, sessions: list) -> tuple[str, object]:
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    relay_on = row.get("relay_enabled", False)
    status = "🟢 Активен" if relay_on else "🔴 Выключен"
    text = (
        f"📨 <b>Inbox {label}</b>\n\n"
        f"Статус: {status}\n"
        f"Диалогов: <b>{len(sessions)}</b>\n\n"
        "Когда включён — сообщения пользователей пересылаются вам.\n"
        "Отвечайте <b>reply</b> на пересланное сообщение чтобы ответить пользователю."
    )
    return text, relay_menu(row["bot_id"], relay_on, sessions)


@router.callback_query(RelayCb.filter(F.action == "menu"))
async def cb_relay_menu(callback: CallbackQuery, callback_data: RelayCb,
                         pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    sessions = await db.get_relay_sessions(pool, callback_data.bot_id)
    text, markup = await _relay_menu_text(row, sessions)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()


@router.callback_query(RelayCb.filter(F.action == "toggle"))
async def cb_relay_toggle(callback: CallbackQuery, callback_data: RelayCb,
                           pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    new_state = not row.get("relay_enabled", False)
    await db.enable_relay(pool, callback_data.bot_id, new_state)
    status = "включён ✅" if new_state else "отключён ❌"
    # Reload row with updated state
    row2 = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    sessions = await db.get_relay_sessions(pool, callback_data.bot_id)
    text, markup = await _relay_menu_text(row2, sessions)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer(f"Inbox {status}")


# ── Operator reply detection — route back to user ─────────────────────────

@router.message(F.reply_to_message & F.text)
async def handle_operator_reply(message: Message, pool: asyncpg.Pool,
                                 http: aiohttp.ClientSession) -> None:
    """When operator replies to a forwarded relay message, send reply to original user."""
    reply_msg_id = message.reply_to_message.message_id
    session = await db.find_session_by_forwarded_msg(pool, reply_msg_id)
    if not session:
        return  # Not a relay reply — ignore

    ok, _ = await bot_api.send_message(http, session["token"], session["user_id"],
                                        message.text)
    if ok:
        sess_row = await pool.fetchrow(
            "SELECT id FROM relay_sessions WHERE bot_id=$1 AND user_id=$2",
            session["bot_id"], session["user_id"],
        )
        if sess_row:
            await db.save_relay_message(pool, sess_row["id"], "out", message.text)
        await message.reply("✅ Ответ доставлен пользователю.")
    else:
        await message.reply("❌ Не удалось доставить ответ — пользователь мог заблокировать бота.")
