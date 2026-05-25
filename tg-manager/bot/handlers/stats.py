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
    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Бот не найден.")
        return
    await callback.message.edit_text("⏳ Загружаю статистику…")

    stats = await db.get_bot_stats(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else (row["first_name"] or str(row["bot_id"]))

    completion_rate = (
        round(stats["funnel_completed"] / stats["funnel_total_subs"] * 100)
        if stats["funnel_total_subs"] else 0
    )

    hint = (
        "\n\n📌 <b>Что это?</b>\n"
        "Статистика показывает рост аудитории, активность рассылок и состояние автоматизации.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "• Следите за приростом аудитории по дням\n"
        "• Оценивайте эффективность рассылок\n"
        "• Отслеживайте активность цепочек и inbox"
    )
    text = (
        f"📊 <b>Статистика — {label}</b>\n\n"
        f"👥 <b>Аудитория:</b> {stats['aud_total']} чел.\n"
        f"  🆕 За сутки: +{stats['aud_today']}\n"
        f"  📈 За 7 дней: +{stats['aud_week']}\n\n"
        f"📢 <b>Рассылки:</b> {stats['broadcasts_total']} всего, "
        f"отправлено {stats['broadcasts_sent']} сообщений\n\n"
        f"💬 <b>Inbox:</b> {stats['relay_sessions']} диалогов\n"
        f"  📩 Входящих: {stats['msg_in']}\n"
        f"  📤 Исходящих: {stats['msg_out']}\n"
        f"  🆕 Новых за 24ч: {stats['relay_today']}\n\n"
        f"🤖 <b>Авто-ответов активных:</b> {stats['active_replies']}\n\n"
        f"🔗 <b>Цепочек активных:</b> {stats['active_funnels']}\n"
        f"  👤 Подписчиков: {stats['funnel_users']}\n"
        f"  ✅ Завершили: {stats['funnel_completed']} ({completion_rate}%)"
        + hint
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_to_bot(callback_data.bot_id),
    )
