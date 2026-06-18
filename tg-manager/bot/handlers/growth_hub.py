"""Growth Engine — система вирального роста BotMother.

5 механик:
  1. 📊 Мой прогресс      — тир амбассадора, прогресс-бар, реф-статистика
  2. 📦 Контент-пакеты    — посты с реальной статистикой платформы → одна кнопка → все каналы
  3. 🏆 Лидерборд         — топ рефереров месяца, мотивирующий рейтинг
  4. 💰 Комиссии & выплаты — баланс, история начислений, заявка на выплату
  5. 🔗 Реферальный пакет — ссылка + готовые тексты + инструкция
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os
from typing import Optional

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, DmCb, GrowthCb
from database import db
from services import account_manager

log = logging.getLogger(__name__)
router = Router()

_MIN_PAYOUT = 5.0   # минимум для выплаты (USD)


def _is_admin(uid: int) -> bool:
    raw = os.getenv("ADMIN_IDS", "")
    return uid in {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


# ─── FSM ──────────────────────────────────────────────────────────────────────

class GrowthFSM(StatesGroup):
    payout_method = State()
    payout_wallet = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Мой прогресс", callback_data=GrowthCb(action="dashboard"))
    kb.button(text="📦 Контент-пакеты", callback_data=GrowthCb(action="content"))
    kb.button(text="🏆 Лидерборд", callback_data=GrowthCb(action="leaderboard"))
    kb.button(text="💰 Комиссии & Выплаты", callback_data=GrowthCb(action="commission"))
    kb.button(text="🔗 Реферальный пакет", callback_data=GrowthCb(action="outreach"))
    kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Центр роста", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


# ─── Entry ────────────────────────────────────────────────────────────────────

@router.message(Command("growth"))
async def cmd_growth(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_menu_text(), parse_mode="HTML", reply_markup=_menu_kb())


@router.callback_query(GrowthCb.filter(F.action == "menu"))
async def cb_growth_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_text(_menu_text(), parse_mode="HTML", reply_markup=_menu_kb())
    except Exception:
        await callback.message.answer(_menu_text(), parse_mode="HTML", reply_markup=_menu_kb())


def _menu_text() -> str:
    return (
        "🚀 <b>Центр роста BotMother</b>\n\n"
        "Здесь вы управляете своим ростом как амбассадор:\n\n"
        "📊 <b>Мой прогресс</b> — тир, рефералы, путь к следующей ступени\n"
        "📦 <b>Контент-пакеты</b> — готовые посты с живой статистикой платформы\n"
        "🏆 <b>Лидерборд</b> — топ реферальных партнёров этого месяца\n"
        "💰 <b>Комиссии</b> — зарабатывайте до 30% с каждого платежа реферала\n"
        "🔗 <b>Реф-пакет</b> — ваша ссылка + готовые тексты для любой площадки"
    )


# ─── 1. Dashboard / Мой прогресс ─────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "dashboard"))
async def cb_growth_dashboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    status = await db.get_ambassador_status(pool, callback.from_user.id)
    ref = status["ref_stats"]
    tier = status["current_tier"]
    nxt = status["next_tier"]
    balance = status["commission_balance"]
    paid_out = status["paid_out"]

    tier_line = "🌑 Нет тира — пригласите первого пользователя!"
    if tier:
        tier_line = f"{tier['tier_emoji']} <b>{tier['tier_name']}</b>"
        if tier.get("commission_pct", 0) > 0:
            tier_line += f" · {tier['commission_pct']}% комиссия"
        if tier.get("badge_label"):
            tier_line += f"\n{tier['badge_label']}"

    # Progress to next tier
    progress_line = ""
    if nxt:
        needed_active = nxt["min_active_refs"]
        needed_paid = nxt["min_paid_refs"]
        if needed_paid > 0:
            done = ref["paid"]
            progress_line = _progress(done, needed_paid, f"до тира {nxt['tier_emoji']} {nxt['tier_name']}: {done}/{needed_paid} платящих")
        elif needed_active > 0:
            done = ref["active"]
            progress_line = _progress(done, needed_active, f"до тира {nxt['tier_emoji']} {nxt['tier_name']}: {done}/{needed_active} активных")

    commission_line = ""
    if balance > 0 or paid_out > 0:
        commission_line = (
            f"\n💰 Баланс комиссии: <b>${balance:.2f}</b>\n"
            f"✅ Выплачено всего: <b>${paid_out:.2f}</b>"
        )

    tiers_preview = "\n".join(
        f"  {t['tier_emoji']} {t['tier_name']}: "
        + (f"{t['min_active_refs']} активных → {t['reward_days']} дней {t['reward_plan']}"
           if t["min_paid_refs"] == 0 and t["reward_days"] > 0
           else f"{t['min_paid_refs']} платящих → {t['commission_pct']}% комиссия"
           if t["commission_pct"] > 0
           else f"{t['min_paid_refs']} платящих → {t['reward_days']} дней {t['reward_plan']}")
        for t in status["tiers"]
    )

    text = (
        f"📊 <b>Мой прогресс</b>\n\n"
        f"Тир: {tier_line}\n\n"
        f"👥 Всего рефералов: <b>{ref['total']}</b>\n"
        f"✅ Активировали: <b>{ref['active']}</b>\n"
        f"💳 Оплатили: <b>{ref['paid']}</b>\n"
        f"{progress_line}"
        f"{commission_line}\n\n"
        f"<b>Лестница тиров:</b>\n{tiers_preview}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Моя реф-ссылка", callback_data=GrowthCb(action="outreach"))
    kb.button(text="💰 Запросить выплату", callback_data=GrowthCb(action="payout_ask"))
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


def _progress(done: int, total: int, label: str) -> str:
    if total <= 0:
        return ""
    pct = min(done / total, 1.0)
    filled = round(10 * pct)
    bar = "█" * filled + "░" * (10 - filled)
    return f"\n[{bar}] {label}\n"


# ─── 2. Контент-пакеты ────────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "content"))
async def cb_growth_content(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    seeds = await db.get_growth_content_seeds(pool)
    if not seeds:
        await callback.answer("Нет контент-пакетов.", show_alert=True)
        return
    stats = await db.get_growth_platform_stats(pool)
    type_emoji = {"stats": "📊", "native": "💡", "direct": "🚀", "case": "📈"}
    kb = InlineKeyboardBuilder()
    for s in seeds:
        emoji = type_emoji.get(s["content_type"], "•")
        kb.button(
            text=f"{emoji} {s['title']}",
            callback_data=GrowthCb(action="content_deploy", item_id=s["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    text = (
        f"📦 <b>Контент-пакеты</b>\n\n"
        f"Каждый пакет использует <b>реальную статистику</b> BotMother:\n"
        f"• {stats['total_users']}+ пользователей\n"
        f"• {stats['total_channels']}+ каналов\n"
        f"• {stats['total_ops']}+ операций\n\n"
        f"Выберите пакет → публикуется в ваших каналах с вашей реф-ссылкой:"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GrowthCb.filter(F.action == "content_deploy"))
async def cb_growth_content_deploy(
    callback: CallbackQuery, callback_data: GrowthCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    seed = await pool.fetchrow(
        "SELECT * FROM growth_content_seeds WHERE id=$1", callback_data.item_id
    )
    if not seed:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    # Build preview with real stats + referral link
    stats = await db.get_growth_platform_stats(pool)
    try:
        me = await callback.bot.get_me()
        code = await db.get_or_create_referral_code(pool, callback.from_user.id)
        ref_link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        ref_link = "https://t.me/BotMotherBot"

    content = seed["template"].format(
        users=f"{int(stats['total_users']):,}".replace(",", " "),
        ops=f"{int(stats['total_ops']):,}".replace(",", " "),
        channels=f"{int(stats['total_channels']):,}".replace(",", " "),
        ref_link=ref_link,
    )
    channels = await db.get_managed_channels(pool, callback.from_user.id)
    ch_count = len(channels)
    preview = content[:300] + ("…" if len(content) > 300 else "")
    text = (
        f"📦 <b>{_html.escape(seed['title'])}</b>\n\n"
        f"<i>Предпросмотр:</i>\n{preview}\n\n"
        f"📡 Каналов для публикации: <b>{ch_count}</b>"
    )
    kb = InlineKeyboardBuilder()
    if ch_count > 0:
        kb.button(
            text=f"🚀 Опубликовать в {ch_count} каналах",
            callback_data=GrowthCb(action="content_confirm", item_id=seed["id"]),
        )
    else:
        text += "\n\n⚠️ <i>Нет управляемых каналов. Добавьте каналы в разделе «📡 Каналы».</i>"
    kb.button(text="◀️ К пакетам", callback_data=GrowthCb(action="content"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GrowthCb.filter(F.action == "content_confirm"))
async def cb_growth_content_confirm(
    callback: CallbackQuery, callback_data: GrowthCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю публикацию…")
    seed = await pool.fetchrow(
        "SELECT * FROM growth_content_seeds WHERE id=$1", callback_data.item_id
    )
    if not seed:
        return
    user_id = callback.from_user.id
    stats = await db.get_growth_platform_stats(pool)
    try:
        me = await callback.bot.get_me()
        code = await db.get_or_create_referral_code(pool, user_id)
        ref_link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        ref_link = "https://t.me/BotMotherBot"

    content = seed["template"].format(
        users=f"{int(stats['total_users']):,}".replace(",", " "),
        ops=f"{int(stats['total_ops']):,}".replace(",", " "),
        channels=f"{int(stats['total_channels']):,}".replace(",", " "),
        ref_link=ref_link,
    )
    channels = await db.get_managed_channels(pool, user_id)

    asyncio.create_task(
        _post_content_bg(callback.bot, pool, user_id, int(seed["id"]), channels, content)
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Другие пакеты", callback_data=GrowthCb(action="content"))
    kb.button(text="◀️ Центр роста", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            f"⏳ <b>Публикация запущена!</b>\n\n"
            f"Рассылаю в <b>{len(channels)}</b> каналов (1.5 сек/канал).\n"
            f"Отчёт придёт по завершении.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


async def _post_content_bg(
    bot: Bot,
    pool: asyncpg.Pool,
    user_id: int,
    seed_id: int,
    channels: list,
    content: str,
) -> None:
    sent = failed = 0
    for ch in channels:
        try:
            acc_row = await db.get_account_for_telethon(pool, ch["acc_id"], user_id)
            if not acc_row or not acc_row["session_str"]:
                failed += 1
                continue
            res = await account_manager.post_to_channel(
                session_string=acc_row["session_str"],
                channel_id=ch["channel_id"],
                text=content,
                access_hash=ch.get("access_hash") or 0,
                _acc=dict(acc_row),
            )
            if res.get("error"):
                failed += 1
            else:
                sent += 1
        except Exception as exc:
            log.warning("growth_hub channel=%s: %s", ch.get("channel_id"), exc)
            failed += 1
        await asyncio.sleep(1.5)
    # Bump deployed count
    await pool.execute(
        "UPDATE growth_content_seeds SET deployed_count=deployed_count+$1 WHERE id=$2",
        sent, seed_id,
    )
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Контент-пакет опубликован!</b>\n\n"
            f"📢 Опубликовано: <b>{sent}</b> каналов\n"
            f"⚠️ Ошибок: <b>{failed}</b>\n\n"
            f"<i>Каждая публикация с вашей реф-ссылкой работает на ваш доход 24/7.</i>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─── 3. Лидерборд ────────────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "leaderboard"))
async def cb_growth_leaderboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    rows = await db.get_referral_leaderboard_monthly(pool, limit=10)
    my_id = callback.from_user.id

    if not rows:
        text = (
            "🏆 <b>Лидерборд — Топ рефереров месяца</b>\n\n"
            "Пока нет участников. Будьте первым!\n\n"
            "Каждый платящий реферал = позиция в рейтинге.\n"
            "Ежемесячно топ-3 получают бонусы от команды BotMother."
        )
    else:
        medals = ["🥇", "🥈", "🥉"] + ["4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        lines = []
        my_pos = None
        for i, r in enumerate(rows):
            name = _html.escape(r["name"] or "Аноним")
            if r.get("username"):
                name = f"@{_html.escape(r['username'])}"
            pos = medals[i] if i < len(medals) else f"{i+1}."
            paid = r["paid_count"]
            active = r["active_count"]
            line = f"{pos} {name} — 💳{paid} платящих · 👥{active} активных"
            if r["referrer_id"] == my_id:
                line = f"<b>{line} ← вы</b>"
                my_pos = i + 1
            lines.append(line)
        my_note = f"\n<i>Ваша позиция: #{my_pos}</i>" if my_pos else "\n<i>Вас нет в топ-10. Пригласите платящих пользователей!</i>"
        text = (
            f"🏆 <b>Лидерборд — Топ рефереров месяца</b>\n\n"
            + "\n".join(lines)
            + f"{my_note}\n\n"
            "<i>Топ-3 каждый месяц получают бонусные выплаты от команды BotMother.</i>"
        )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Моя реф-ссылка", callback_data=GrowthCb(action="outreach"))
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ─── 4. Комиссии & Выплаты ────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "commission"))
async def cb_growth_commission(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    status = await db.get_ambassador_status(pool, user_id)
    balance = status["commission_balance"]
    paid_out = status["paid_out"]
    history = await db.get_commission_history(pool, user_id, limit=8)

    tier = status["current_tier"]
    comm_pct = float(tier["commission_pct"]) if tier and tier.get("commission_pct") else 0.0
    tier_note = (
        f"Ваш тир: {tier['tier_emoji']} {tier['tier_name']} · <b>{comm_pct:.0f}%</b> комиссия\n\n"
        if tier and comm_pct > 0
        else "💡 Достигните тира 🥈 Серебро (5 платящих рефералов) — и начнёте получать 5% с каждого платежа.\n\n"
    )
    hist_lines = []
    for h in history:
        ts = h["created_at"].strftime("%d.%m")
        status_icon = "✅" if h["status"] == "paid" else "⏳"
        hist_lines.append(
            f"{status_icon} {ts} — {h['ref_name']}: "
            f"${h['payment_amount']:.2f} × {h['commission_pct']:.0f}% = <b>${h['commission_usd']:.2f}</b>"
        )
    hist_block = "\n".join(hist_lines) if hist_lines else "<i>Начислений пока нет.</i>"

    text = (
        f"💰 <b>Комиссии & Выплаты</b>\n\n"
        f"{tier_note}"
        f"💵 Баланс к выплате: <b>${balance:.2f}</b>\n"
        f"✅ Выплачено всего: <b>${paid_out:.2f}</b>\n\n"
        f"<b>Последние начисления:</b>\n{hist_block}"
    )
    kb = InlineKeyboardBuilder()
    if balance >= _MIN_PAYOUT:
        kb.button(
            text=f"💸 Запросить выплату ${balance:.2f}",
            callback_data=GrowthCb(action="payout_ask"),
        )
    else:
        kb.button(
            text=f"💸 Минимум ${_MIN_PAYOUT:.0f} для выплаты (у вас ${balance:.2f})",
            callback_data=GrowthCb(action="commission"),  # no-op, just info
        )
    kb.button(text="📊 Мой прогресс", callback_data=GrowthCb(action="dashboard"))
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GrowthCb.filter(F.action == "payout_ask"))
async def cb_growth_payout_ask(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    status = await db.get_ambassador_status(pool, user_id)
    balance = status["commission_balance"]
    if balance < _MIN_PAYOUT:
        await callback.answer(
            f"Минимальная сумма для выплаты — ${_MIN_PAYOUT:.0f}. "
            f"Ваш баланс: ${balance:.2f}",
            show_alert=True,
        )
        return
    await state.set_state(GrowthFSM.payout_method)
    await state.update_data(amount=balance)
    kb = InlineKeyboardBuilder()
    kb.button(text="USDT TRC-20", callback_data=GrowthCb(action="payout_confirm", item_id=1))
    kb.button(text="TON", callback_data=GrowthCb(action="payout_confirm", item_id=2))
    kb.button(text="◀️ Отмена", callback_data=GrowthCb(action="commission"))
    kb.adjust(2, 1)
    try:
        await callback.message.edit_text(
            f"💸 <b>Запрос выплаты ${balance:.2f}</b>\n\nВыберите способ получения:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


@router.callback_query(GrowthCb.filter(F.action == "payout_confirm"))
async def cb_growth_payout_method(
    callback: CallbackQuery, callback_data: GrowthCb, state: FSMContext
) -> None:
    await callback.answer()
    method_map = {1: "usdt_trc20", 2: "ton"}
    method = method_map.get(callback_data.item_id, "usdt_trc20")
    label = "USDT TRC-20" if method == "usdt_trc20" else "TON"
    await state.update_data(method=method)
    await state.set_state(GrowthFSM.payout_wallet)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=GrowthCb(action="commission"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            f"Способ: <b>{label}</b>\n\nВведите адрес кошелька:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


@router.message(GrowthFSM.payout_wallet)
async def fsm_growth_payout_wallet(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    wallet = (message.text or "").strip()
    if not wallet or len(wallet) < 10:
        await message.answer("Адрес слишком короткий. Введите корректный кошелёк:")
        return
    data = await state.get_data()
    amount = data.get("amount", 0.0)
    method = data.get("method", "usdt_trc20")
    await state.clear()
    req_id = await db.create_payout_request(pool, message.from_user.id, amount, method, wallet)
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Мои комиссии", callback_data=GrowthCb(action="commission"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Заявка на выплату #{req_id} создана!</b>\n\n"
        f"Сумма: <b>${amount:.2f}</b>\n"
        f"Кошелёк: <code>{_html.escape(wallet)}</code>\n\n"
        f"Команда BotMother обработает выплату в течение 1-3 рабочих дней.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    # Notify admins
    raw = os.getenv("ADMIN_IDS", "")
    admin_ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    for admin_id in admin_ids:
        try:
            await message.bot.send_message(
                admin_id,
                f"💸 <b>Новая заявка на выплату #{req_id}</b>\n\n"
                f"Пользователь: {message.from_user.id}\n"
                f"Сумма: <b>${amount:.2f}</b>\n"
                f"Метод: {method}\n"
                f"Кошелёк: <code>{_html.escape(wallet)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─── 5. Реферальный пакет ────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "outreach"))
async def cb_growth_outreach(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    try:
        me = await callback.bot.get_me()
        code = await db.get_or_create_referral_code(pool, user_id)
        ref_link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        ref_link = "https://t.me/BotMotherBot"
        code = "?"

    status = await db.get_ambassador_status(pool, user_id)
    tier = status["current_tier"]
    comm_note = (
        f"\n💰 Ваша комиссия: <b>{float(tier['commission_pct']):.0f}%</b> с каждого платежа"
        if tier and float(tier.get("commission_pct") or 0) > 0
        else "\n💡 Достигните 5 платящих рефералов → 5% комиссия с их платежей навсегда"
    )

    text = (
        f"🔗 <b>Реферальный пакет</b>\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n"
        f"{comm_note}\n\n"
        f"<b>Готовые тексты:</b>\n\n"
        f"<b>1. Короткий (для подписи, биографии):</b>\n"
        f"<code>Автоматизирую Telegram через BotMother → {ref_link}</code>\n\n"
        f"<b>2. Для группы / чата:</b>\n"
        f"<code>Если управляете Telegram-каналами — попробуйте BotMother. "
        f"Автоматизация рассылок, прогрев аккаунтов, DM-кампании. "
        f"Первые 7 дней бесплатно: {ref_link}</code>\n\n"
        f"<b>3. Для поста в канале:</b>\n"
        f"<code>Инструмент, который изменил мой подход к Telegram-маркетингу. "
        f"BotMother — управляй сотнями каналов из одного места, "
        f"автоматизируй рассылки и DM. Я использую сам: {ref_link}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Опубликовать контент-пакет", callback_data=GrowthCb(action="content"))
    kb.button(text="📊 Мой прогресс", callback_data=GrowthCb(action="dashboard"))
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
