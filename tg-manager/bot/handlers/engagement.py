"""User activity tracking and re-engagement broadcasts."""
from __future__ import annotations
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import EngageCb, CrmCb
from bot.keyboards import engagement_menu, back_to_bot, subscription_locked_markup
from bot.utils.subscription import require_plan, locked_text
from bot.states import ReactivateBroadcast
from database import db
from services import broadcaster

router = Router()


def _heatmap_chart(data: list[dict]) -> str:
    if not data:
        return "Нет данных за этот период."
    hour_counts = {d["hour"]: d["count"] for d in data}
    max_count = max(hour_counts.values()) if hour_counts else 1
    lines = []
    for h in range(24):
        cnt = hour_counts.get(h, 0)
        bar_len = round(cnt / max_count * 12) if max_count else 0
        bar = "█" * bar_len + "░" * (12 - bar_len)
        lines.append(f"{h:02d}:00 {bar} {cnt}")
    return "\n".join(lines)


@router.callback_query(EngageCb.filter(F.action == "menu"))
async def cb_engage_menu(callback: CallbackQuery, callback_data: EngageCb,
                          pool: asyncpg.Pool) -> None:

    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Активность и реактивация", "pro"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    segs = await db.get_activity_segments(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    total = segs["total"]
    pct_hot  = round(segs["hot"]  / total * 100) if total else 0
    pct_warm = round(segs["warm"] / total * 100) if total else 0
    pct_cold = round(segs["cold"] / total * 100) if total else 0
    pct_lost = round(segs["lost"] / total * 100) if total else 0
    await callback.message.edit_text(
        f"🎯 <b>Активность — {safe_label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Система делит вашу аудиторию на 4 группы по тому, когда они последний раз писали боту. Вы можете отправить специальное сообщение именно тем, кто давно не заходил, и вернуть их.\n\n"
        f"Всего отслеживается: <b>{total}</b>\n\n"
        f"🔥 Горячие (&lt;24ч):     <b>{segs['hot']}</b> ({pct_hot}%) — активны сейчас\n"
        f"🌡 Тёплые (1–7 дн):     <b>{segs['warm']}</b> ({pct_warm}%) — недавно заходили\n"
        f"❄️ Холодные (7–30 дн):  <b>{segs['cold']}</b> ({pct_cold}%) — давно нет\n"
        f"💀 Потерянные (30+ дн): <b>{segs['lost']}</b> ({pct_lost}%) — почти ушли\n\n"
        "<i>💡 Совет: реактивируйте холодных и потерянных — это поднимает engagement rate в поиске.</i>",
        parse_mode="HTML",
        reply_markup=engagement_menu(callback_data.bot_id, segs),
    )


@router.callback_query(EngageCb.filter(F.action.in_({"segment_hot", "segment_warm"})))
async def cb_engage_info(callback: CallbackQuery, callback_data: EngageCb) -> None:
    labels = {
        "segment_hot":  "🔥 Горячие — были активны < 24ч. Они сейчас в боте!",
        "segment_warm": "🌡 Тёплые — были активны 1–7 дней назад. Поддерживайте интерес.",
    }
    await callback.answer(labels.get(callback_data.action, ""), show_alert=True)


@router.callback_query(EngageCb.filter(F.action == "reactivate_cold"))
async def cb_reactivate_cold(callback: CallbackQuery, callback_data: EngageCb,
                               state: FSMContext) -> None:
    await state.set_state(ReactivateBroadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id, segment="cold")
    await callback.message.edit_text(
        "❄️ <b>Реактивация холодных (7–30 дн)</b>\n\n"
        "Пользователи не заходили от 7 до 30 дней.\n\n"
        "Напишите текст реактивационного сообщения:\n\n"
        "<i>Примеры:\n"
        "• «Привет! Давно не виделись 👋 У нас появилось кое-что новое!»\n"
        "• «Соскучились по тебе! Возвращайся — специальное предложение ждёт»</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(EngageCb.filter(F.action == "reactivate_lost"))
async def cb_reactivate_lost(callback: CallbackQuery, callback_data: EngageCb,
                               state: FSMContext) -> None:
    await state.set_state(ReactivateBroadcast.waiting_message)
    await state.update_data(bot_id=callback_data.bot_id, segment="lost")
    await callback.message.edit_text(
        "💀 <b>Реактивация потерянных (30+ дн)</b>\n\n"
        "Пользователи не заходили более месяца.\n\n"
        "Напишите текст реактивационного сообщения:\n\n"
        "<i>Примеры:\n"
        "• «Давно не виделись! Мы изменились — загляни и убедись 🚀»\n"
        "• «Последний шанс! Не потеряй доступ к [название функции]»</i>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ReactivateBroadcast.waiting_message, F.text)
async def msg_reactivate(message: Message, state: FSMContext,
                          pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    await state.clear()
    bot_id = data["bot_id"]
    segment = data["segment"]

    row = await db.get_bot(pool, bot_id, message.from_user.id)
    if not row:
        await message.answer("Бот не найден.")
        return

    if segment == "cold":
        user_ids = await db.get_inactive_user_ids(pool, bot_id, 7, 30)
        seg_label = "❄️ холодных (7–30 дн)"
    else:
        user_ids = await db.get_inactive_user_ids(pool, bot_id, 30)
        seg_label = "💀 потерянных (30+ дн)"

    if not user_ids:
        await message.answer(
            f"Нет {seg_label} пользователей в базе активности.\n"
            "Данные накапливаются по мере работы бота.",
            reply_markup=back_to_bot(bot_id),
        )
        return

    bc_id = await db.create_broadcast(pool, bot_id, message.text, len(user_ids), message.from_user.id)
    await message.answer(
        f"✅ <b>Реактивация запущена!</b>\n\n"
        f"Сегмент: {seg_label}\n"
        f"Получателей: <b>{len(user_ids)}</b>\n\n"
        "Рассылка идёт в фоне.",
        parse_mode="HTML",
        reply_markup=back_to_bot(bot_id),
    )
    # broadcaster.start() is sync — it schedules asyncio.Task internally
    broadcaster.start(pool, http, bc_id, row["token"], bot_id,
                      message.text, None, user_ids, None)


@router.callback_query(EngageCb.filter(F.action == "heatmap"))
async def cb_engage_heatmap(callback: CallbackQuery, callback_data: EngageCb,
                              pool: asyncpg.Pool) -> None:

    await callback.answer()
    data = await db.get_activity_heatmap(pool, callback_data.bot_id, days=7)
    chart = _heatmap_chart(data)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=EngageCb(action="menu", bot_id=callback_data.bot_id))
    await callback.message.edit_text(
        "📊 <b>Тепловая карта активности (7 дней)</b>\n\n"
        f"<code>{chart}</code>\n\n"
        "Часовой пояс: UTC. Планируйте рассылки в пиковые часы.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(EngageCb.filter(F.action == "top_users"))
async def cb_engage_top(callback: CallbackQuery, callback_data: EngageCb,
                         pool: asyncpg.Pool) -> None:

    await callback.answer()
    users = await db.get_top_active_users(pool, callback_data.bot_id, limit=10)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=EngageCb(action="menu", bot_id=callback_data.bot_id))
    medals = ["🥇", "🥈", "🥉"] + ["🎖"] * 10
    if users:
        lines = [
            f"{medals[i]} <code>{u['user_id']}</code> — {u['message_count']} сообщ. "
            f"(посл. {u['last_seen'].strftime('%d.%m')})"
            for i, u in enumerate(users)
        ]
        body = "\n".join(lines)
    else:
        body = "Нет данных. Активность накапливается по мере работы бота."
    await callback.message.edit_text(
        f"🏆 <b>Топ-10 активных пользователей</b>\n\n{body}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(EngageCb.filter(F.action == "autotag"))
async def cb_engage_autotag(callback: CallbackQuery, callback_data: EngageCb,
                              pool: asyncpg.Pool) -> None:
    await callback.answer("⏳ Присваиваю теги...")
    segs = await db.autotag_by_activity(pool, callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏷 Открыть CRM",
              callback_data=CrmCb(action="menu", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад",
              callback_data=EngageCb(action="menu", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Авто-теги по активности назначены!</b>\n\n"
        f"🔥 activity:hot  → {segs['hot']} польз.\n"
        f"🌡 activity:warm → {segs['warm']} польз.\n"
        f"❄️ activity:cold → {segs['cold']} польз.\n"
        f"💀 activity:lost → {segs['lost']} польз.\n\n"
        "Теги доступны в <b>CRM</b> → делайте таргетированные рассылки по сегментам.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
