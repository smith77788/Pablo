"""Per-bot statistics handler."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
import asyncpg
from bot.callbacks import StatsCb, BotCb
from bot.keyboards import back_to_bot
from database import db

router = Router()


@router.callback_query(StatsCb.filter(F.action == "menu"))
async def cb_stats_menu(callback: CallbackQuery, callback_data: StatsCb,
                         pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю статистику…")

    stats = await db.get_bot_stats(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    completion_rate = round(stats["funnel_completed"] / stats["funnel_total_subs"] * 100) if stats["funnel_total_subs"] else 0

    text = (
        f"📊 <b>Статистика — {label}</b>\n\n"
        f"👥 <b>Пользователи в Inbox:</b> {stats['relay_sessions']}\n"
        f"📩 Сообщений получено: {stats['msg_in']}\n"
        f"📤 Ответов отправлено: {stats['msg_out']}\n"
        f"🆕 Новых диалогов за 24ч: {stats['relay_today']}\n\n"
        f"🤖 <b>Авто-ответы активных:</b> {stats['active_replies']}\n\n"
        f"🔗 <b>Цепочек активных:</b> {stats['active_funnels']}\n"
        f"👤 Подписчиков в цепочках: {stats['funnel_users']}\n"
        f"✅ Завершили цепочку: {stats['funnel_completed']} ({completion_rate}%)"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_to_bot(callback_data.bot_id),
    )
