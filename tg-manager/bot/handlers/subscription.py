"""Subscription plan selection and crypto payment flow."""
from __future__ import annotations
import random
import string
import asyncpg
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import SubCb
from bot.utils.subscription import get_plan, PLAN_LEVELS, PLAN_EMOJIS, PLAN_FEATURES, BOT_LIMITS
from config import PLAN_PRICES_USD, PERIOD_DISCOUNTS, TON_WALLET, TRON_WALLET

router = Router()

_TON_RATE = 3.0  # 1 TON ≈ $3


def _gen_ref() -> str:
    return "PAY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _calc(plan: str, months: int, currency: str) -> tuple[float, float]:
    """Returns (usd_total, crypto_amount)."""
    base = PLAN_PRICES_USD.get(plan, 0)
    disc = PERIOD_DISCOUNTS.get(months, 0)
    usd = round(base * months * (1 - disc / 100), 2)
    if currency == "TON":
        return usd, round(usd / _TON_RATE, 2)
    return usd, usd  # USDT 1:1


async def _show_menu(target, pool: asyncpg.Pool, user_id: int) -> None:
    plan = await get_plan(pool, user_id)
    lim = BOT_LIMITS.get(plan, 3)
    lim_label = "∞" if lim >= 9999 else str(lim)
    emoji = PLAN_EMOJIS.get(plan, "🆓")
    text = (
        f"💳 <b>Подписка</b>\n\n"
        f"Текущий план: <b>{emoji} {plan.upper()}</b> · до {lim_label} ботов\n\n"
        f"<b>Доступные планы:</b>\n\n"
        f"⭐ <b>STARTER</b> — $9/мес · до 10 ботов\n"
        f"<i>{PLAN_FEATURES['starter']}</i>\n\n"
        f"🚀 <b>PRO</b> — $25/мес · до 30 ботов\n"
        f"<i>{PLAN_FEATURES['pro']}</i>\n\n"
        f"👑 <b>ENTERPRISE</b> — $69/мес · неограниченно\n"
        f"<i>{PLAN_FEATURES['enterprise']}</i>"
    )
    kb = InlineKeyboardBuilder()
    for p in ("starter", "pro", "enterprise"):
        price = PLAN_PRICES_USD[p]
        em = PLAN_EMOJIS[p]
        prefix = "✅ " if plan == p else ""
        kb.button(
            text=f"{prefix}{em} {p.upper()} — ${price}/мес",
            callback_data=SubCb(action="choose_plan", plan=p),
        )
    kb.adjust(1)
    markup = kb.as_markup()
    if hasattr(target, "edit_text"):
        await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, pool: asyncpg.Pool) -> None:
    await _show_menu(message, pool, message.from_user.id)


@router.callback_query(SubCb.filter(F.action == "menu"))
async def cb_sub_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await _show_menu(callback.message, pool, callback.from_user.id)


@router.callback_query(SubCb.filter(F.action == "choose_plan"))
async def cb_choose_plan(callback: CallbackQuery, callback_data: SubCb) -> None:
    await callback.answer()
    plan = callback_data.plan
    if plan not in PLAN_PRICES_USD:
        await callback.answer("Неизвестный план.", show_alert=True)
        return
    base = PLAN_PRICES_USD[plan]
    em = PLAN_EMOJIS.get(plan, "")
    kb = InlineKeyboardBuilder()
    for months, disc in [(1, 0), (3, 10), (6, 15), (12, 20)]:
        total = round(base * months * (1 - disc / 100), 2)
        disc_txt = f" (-{disc}%)" if disc else ""
        kb.button(
            text=f"{months} мес. — ${total}{disc_txt}",
            callback_data=SubCb(action="choose_period", plan=plan, months=months),
        )
    kb.button(text="◀️ Назад", callback_data=SubCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"💳 {em} <b>{plan.upper()}</b>\n\nБазовая цена: <b>${base}/мес</b>\n\nВыберите период:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SubCb.filter(F.action == "choose_period"))
async def cb_choose_period(callback: CallbackQuery, callback_data: SubCb) -> None:
    await callback.answer()
    plan, months = callback_data.plan, callback_data.months
    kb = InlineKeyboardBuilder()
    if TON_WALLET:
        kb.button(
            text="💎 TON",
            callback_data=SubCb(action="pay", plan=plan, months=months, currency="TON"),
        )
    if TRON_WALLET:
        kb.button(
            text="💵 USDT (TRC-20)",
            callback_data=SubCb(action="pay", plan=plan, months=months, currency="USDT_TRC20"),
        )
    if not TON_WALLET and not TRON_WALLET:
        await callback.answer("Оплата временно недоступна. Свяжитесь с поддержкой.", show_alert=True)
        return
    kb.button(text="◀️ Назад", callback_data=SubCb(action="choose_plan", plan=plan))
    kb.adjust(1)
    usd, _ = _calc(plan, months, "TON")
    await callback.message.edit_text(
        f"💳 <b>{plan.upper()} × {months} мес.</b>\n\nИтого: <b>${usd}</b>\n\nВыберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SubCb.filter(F.action == "pay"))
async def cb_pay(callback: CallbackQuery, callback_data: SubCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    plan, months, currency = callback_data.plan, callback_data.months, callback_data.currency
    wallet = TON_WALLET if currency == "TON" else TRON_WALLET
    if not wallet:
        await callback.answer("Оплата временно недоступна.", show_alert=True)
        return

    usd, crypto = _calc(plan, months, currency)

    for _ in range(5):
        ref = _gen_ref()
        if not await pool.fetchrow("SELECT id FROM payments WHERE reference=$1", ref):
            break

    await pool.execute(
        """
        INSERT INTO payments (user_id, plan, period_months, currency, amount_crypto, amount_usd,
                              wallet_address, reference)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (reference) DO NOTHING
        """,
        callback.from_user.id, plan, months, currency, crypto, usd, wallet, ref,
    )

    if currency == "TON":
        crypto_str = f"{crypto:.2f} TON"
        note = (
            f"⚠️ <b>Укажите комментарий (обязательно):</b>\n"
            f"<code>{ref}</code>\n\n"
            "Без комментария оплата не будет подтверждена автоматически."
        )
    else:
        crypto_str = f"{crypto:.2f} USDT"
        note = "Переведите точную сумму. Сеть: TRC-20 (TRON)."

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить статус", callback_data=SubCb(action="check_status"))
    kb.button(text="◀️ Назад к планам", callback_data=SubCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"💳 <b>Оплата {plan.upper()} на {months} мес.</b>\n\n"
        f"Сумма: <b>{crypto_str}</b> (≈ ${usd})\n"
        f"Адрес: <code>{wallet}</code>\n\n"
        f"{note}\n\n"
        f"⏱ Ожидание: 30 минут\n"
        f"Подписка активируется автоматически после подтверждения в блокчейне.\n\n"
        f"<i>ID платежа: {ref}</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SubCb.filter(F.action == "check_status"))
async def cb_check_status(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    row = await pool.fetchrow(
        "SELECT * FROM payments WHERE user_id=$1 "
        "AND status IN ('pending','confirming','confirmed') "
        "ORDER BY created_at DESC LIMIT 1",
        callback.from_user.id,
    )
    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="💳 К планам", callback_data=SubCb(action="menu"))
        await callback.message.edit_text(
            "❌ Активный платёж не найден.\n\nИспользуйте /subscription для оформления.",
            reply_markup=kb.as_markup(),
        )
        return

    labels = {
        "pending": "⏳ Ожидает оплаты",
        "confirming": "🔄 Подтверждается...",
        "confirmed": "✅ Подтверждён!",
    }
    status = labels.get(row["status"], row["status"])
    kb = InlineKeyboardBuilder()
    if row["status"] in ("pending", "confirming"):
        kb.button(text="🔄 Обновить", callback_data=SubCb(action="check_status"))
    kb.button(text="◀️ Назад", callback_data=SubCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"💳 <b>Статус платежа</b>\n\n"
        f"Статус: {status}\n"
        f"План: <b>{row['plan'].upper()}</b>\n"
        f"Период: {row['period_months']} мес.\n"
        f"Сумма: {row['amount_crypto']} {row['currency']}\n"
        f"Референс: <code>{row['reference']}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
