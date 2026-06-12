"""Per-bot statistics handler."""

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from bot.callbacks import StatsCb, BotCb
from bot.keyboards import back_to_bot
from database import db

router = Router()


@router.callback_query(StatsCb.filter(F.action == "menu"))
async def cb_stats_menu(
    callback: CallbackQuery, callback_data: StatsCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Мои боты", callback_data=BotCb(action="list"))
        kb.adjust(1)
        await callback.message.edit_text(
            "❌ Бот не найден.", reply_markup=kb.as_markup()
        )
        return
    await callback.message.edit_text("⏳ Загружаю статистику…")

    stats = await db.get_bot_stats(pool, callback_data.bot_id)
    daily_growth = await db.get_audience_daily_growth(
        pool, callback_data.bot_id, days=7
    )

    label = (
        f"@{row.get('username')}"
        if row.get("username")
        else (row.get("first_name") or str(row.get("bot_id", "")))
    )

    completion_rate = (
        round(stats["funnel_completed"] / stats["funnel_total_subs"] * 100)
        if stats["funnel_total_subs"]
        else 0
    )

    # Build 5-cell trend bar: ▓ = filled, ░ = empty
    # Each cell represents one day (last 5 days), fill based on % of weekly max
    daily_counts = [d["count"] for d in daily_growth]
    weekly_max = max(daily_counts) if daily_counts else 0
    # Use last 5 days for the bar
    last5 = daily_counts[-5:] if len(daily_counts) >= 5 else daily_counts
    # Pad left with zeros if fewer than 5 days
    last5 = [0] * (5 - len(last5)) + last5
    if weekly_max > 0:
        trend_bar = "".join("▓" if (v / weekly_max) >= 0.2 else "░" for v in last5)
    else:
        trend_bar = "░░░░░"

    today_new = stats["aud_today"]

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
        f"  🆕 Новых за 24ч: <b>+{today_new}</b>\n"
        f"  📈 За 7 дней: +{stats['aud_week']}\n"
        f"  📅 Рост (5 дн): <code>{trend_bar}</code>\n\n"
        f"📢 <b>Рассылки:</b> {stats['broadcasts_total']} всего, "
        f"отправлено {stats['broadcasts_sent']} сообщений\n\n"
        f"💬 <b>Inbox:</b> {stats['relay_sessions']} диалогов\n"
        f"  📩 Входящих: {stats['msg_in']}\n"
        f"  📤 Исходящих: {stats['msg_out']}\n"
        f"  🆕 Новых за 24ч: {stats['relay_today']}\n\n"
        f"🤖 <b>Авто-ответов активных:</b> {stats['active_replies']}\n\n"
        f"🔗 <b>Цепочек активных:</b> {stats['active_funnels']}\n"
        f"  👤 Подписчиков: {stats['funnel_users']}\n"
        f"  ✅ Завершили: {stats['funnel_completed']} ({completion_rate}%)\n"
        f"  🚫 Отписались/заблокировали: {stats.get('funnel_dropped', 0)}" + hint
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=back_to_bot(callback_data.bot_id),
    )
