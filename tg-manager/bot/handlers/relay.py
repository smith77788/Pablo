"""Hermes Relay inbox handler — manage relay, route operator replies back to users."""

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import RelayCb
from bot.keyboards import relay_menu, relay_session_view
from database import db
from services import bot_api

router = Router()


async def _relay_menu_text(row: asyncpg.Record, sessions: list) -> tuple[str, object]:
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    relay_on = row.get("relay_enabled", False)
    status = "🟢 Активен" if relay_on else "🔴 Выключен"
    text = (
        f"📨 <b>Inbox — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Inbox — это как личная переписка через вашего бота. Когда пользователь пишет боту, его сообщение пересылается вам. Вы отвечаете — и ответ уходит пользователю от имени бота. Это живое общение, но через бота.\n\n"
        "💡 <b>Как пользоваться:</b>\n"
        "Включите Inbox → пользователи пишут боту → вы получаете их сообщения → отвечайте «ответом на сообщение» (Reply) прямо здесь.\n\n"
        f"Статус: {status}\n"
        f"Открытых диалогов: <b>{len(sessions)}</b>"
    )
    return text, relay_menu(row["bot_id"], relay_on, sessions)


@router.callback_query(RelayCb.filter(F.action == "menu"))
async def cb_relay_menu(
    callback: CallbackQuery, callback_data: RelayCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    sessions = await db.get_relay_sessions(pool, callback_data.bot_id)
    text, markup = await _relay_menu_text(row, sessions)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(RelayCb.filter(F.action == "toggle"))
async def cb_relay_toggle(
    callback: CallbackQuery, callback_data: RelayCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    new_state = not row.get("relay_enabled", False)
    await db.enable_relay(pool, callback_data.bot_id, new_state)
    status = "включён ✅" if new_state else "отключён ❌"
    await callback.answer(f"Inbox {status}")
    # Reload row with updated state
    row2 = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    sessions = await db.get_relay_sessions(pool, callback_data.bot_id)
    text, markup = await _relay_menu_text(row2, sessions)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


# ── Operator reply detection — route back to user ─────────────────────────


@router.message(F.reply_to_message & F.text)
async def handle_operator_reply(
    message: Message, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    """When operator replies to a forwarded relay message, send reply to original user."""
    reply_msg_id = message.reply_to_message.message_id
    session = await db.find_session_by_forwarded_msg(pool, reply_msg_id)
    if not session:
        return  # Not a relay reply — ignore

    ok, _ = await bot_api.send_message(
        http, session["token"], session["user_id"], message.text
    )
    if ok:
        sess_row = await pool.fetchrow(
            "SELECT id FROM relay_sessions WHERE bot_id=$1 AND user_id=$2",
            session["bot_id"],
            session["user_id"],
        )
        if sess_row:
            await db.save_relay_message(pool, sess_row["id"], "out", message.text)
        await message.reply("✅ Ответ доставлен пользователю.")
    else:
        await message.reply(
            "❌ Не удалось доставить ответ — пользователь мог заблокировать бота."
        )


@router.callback_query(RelayCb.filter(F.action == "session"))
async def cb_relay_session(
    callback: CallbackQuery, callback_data: RelayCb, pool: asyncpg.Pool
) -> None:
    """View message history for a specific relay session."""
    session_id = callback_data.session_id
    # Get session info
    sess = await pool.fetchrow(
        """SELECT rs.*, mb.username as bot_username, mb.first_name as bot_name
           FROM relay_sessions rs
           JOIN managed_bots mb ON mb.bot_id=rs.bot_id
           WHERE rs.id=$1 AND mb.added_by=$2""",
        session_id,
        callback.from_user.id,
    )
    if not sess:
        await callback.answer("Диалог не найден.", show_alert=True)
        return

    await callback.answer()
    messages = await db.get_relay_session_messages(pool, session_id, limit=10)

    user_label = (
        f"@{sess['username']}"
        if sess.get("username")
        else (sess.get("first_name") or str(sess["user_id"]))
    )
    bot_label = (
        f"@{sess['bot_username']}"
        if sess.get("bot_username")
        else sess.get("bot_name", "бот")
    )

    if messages:
        history = []
        for msg in reversed(messages):  # chronological order
            arrow = "📩" if msg["direction"] == "in" else "📤"
            safe_msg = (
                msg["message_text"][:100]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            history.append(f"{arrow} {safe_msg}")
        history_text = "\n".join(history)
    else:
        history_text = "(нет сообщений)"

    safe_user = (
        user_label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_bot = bot_label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = (
        f"💬 <b>Диалог с {safe_user}</b>\n"
        f"Бот: {safe_bot}\n\n"
        f"<b>Последние сообщения:</b>\n{history_text}"
    )
    try:
        templates = await db.get_templates(pool, callback.from_user.id)
    except Exception:
        templates = []
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=relay_session_view(callback_data.bot_id, session_id, templates),
    )


@router.callback_query(RelayCb.filter(F.action == "quick_reply"))
async def cb_relay_quick_reply(
    callback: CallbackQuery,
    callback_data: RelayCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    """Send a template message to the user as operator quick reply."""
    template = await db.get_template(
        pool, callback_data.template_id, callback.from_user.id
    )
    if not template:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return

    # Get session details
    sess = await pool.fetchrow(
        """SELECT rs.*, mb.token
           FROM relay_sessions rs
           JOIN managed_bots mb ON mb.bot_id=rs.bot_id
           WHERE rs.id=$1 AND mb.added_by=$2""",
        callback_data.session_id,
        callback.from_user.id,
    )
    if not sess:
        await callback.answer("Диалог не найден.", show_alert=True)
        return

    ok, _ = await bot_api.send_message(
        http, sess["token"], sess["user_id"], template["text"]
    )
    if ok:
        await db.save_relay_message(
            pool, callback_data.session_id, "out", template["text"]
        )
        await callback.answer(f"✅ «{template['name']}» отправлен!")
    else:
        await callback.answer(
            "❌ Не удалось отправить — пользователь мог заблокировать бота.",
            show_alert=True,
        )


@router.callback_query(RelayCb.filter(F.action == "close_session"))
async def cb_relay_close_session(
    callback: CallbackQuery, callback_data: RelayCb, pool: asyncpg.Pool
) -> None:
    """Delete a relay session (and its messages via CASCADE)."""
    # Verify ownership
    sess = await pool.fetchrow(
        """SELECT rs.id FROM relay_sessions rs
           JOIN managed_bots mb ON mb.bot_id=rs.bot_id
           WHERE rs.id=$1 AND mb.added_by=$2""",
        callback_data.session_id,
        callback.from_user.id,
    )
    if not sess:
        await callback.answer("Диалог не найден.", show_alert=True)
        return

    await db.close_relay_session(pool, callback_data.session_id)
    await callback.answer("🗑 Диалог закрыт")

    # Reload inbox menu
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    sessions = await db.get_relay_sessions(pool, callback_data.bot_id)
    text, markup = await _relay_menu_text(row, sessions)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
