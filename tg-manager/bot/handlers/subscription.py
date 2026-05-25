"""Subscription plan selection and crypto payment flow."""
from __future__ import annotations
import os
import random
import string
import asyncpg
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import SubCb
from bot.utils.subscription import get_plan, PLAN_LEVELS, PLAN_EMOJIS, PLAN_FEATURES, BOT_LIMITS
from config import PLAN_PRICES_USD, PERIOD_DISCOUNTS

# Детальные фичи для каждого плана
PLAN_DETAILED_FEATURES: dict[str, list[str]] = {
    "free": [
        "🤖 До 3 ботов",
        "👥 Управление аудиторией",
        "📢 Рассылки (без ограничений по объёму)",
        "⏰ Расписание рассылок",
        "🤖 Команды бота",
        "📝 Шаблоны сообщений",
        "🌐 Вебхуки",
        "📊 Базовая статистика",
    ],
    "starter": [
        "🤖 До 10 ботов",
        "✅ Всё из FREE",
        "📨 Inbox (live-чат с пользователями)",
        "🏷 CRM и теги",
        "🤖 Автоматизация (правила и триггеры)",
        "🔗 Цепочки сообщений (воронки)",
        "🌍 Мультигео (разные имена/описания по языку)",
        "🔗 Диплинки с аналитикой",
        "📈 SEO-анализ профиля бота",
        "🔄 Клонирование настроек между ботами",
    ],
    "pro": [
        "🤖 До 30 ботов",
        "✅ Всё из STARTER",
        "🧪 A/B тесты сообщений",
        "🎯 Аналитика активности (горячие/холодные/потерянные)",
        "🌐 Массовые операции по всей сети ботов",
        "📢 Сетевые рассылки (по нескольким ботам)",
        "📊 Аналитика сети ботов",
        "🏆 Рейтинг и позиции ботов",
        "📱 Личные аккаунты (Telegram-аккаунты)",
        "👥 Пересечение аудиторий",
    ],
    "enterprise": [
        "🤖 Без ограничений на количество ботов",
        "✅ Всё из PRO",
        "🧬 Swarm — умное роутинг-распределение",
        "🌐 Кластеры ботов и управление сетью",
        "📢 Сетевая рассылка v2 (дедупликация, сегментация)",
        "🔄 Клонирование с полным переносом настроек",
        "🤖 AI-ассистент для анализа ботов",
        "⚖️ Веса роутинга и балансировка нагрузки",
        "📊 Полная аналитика и экспорт данных",
        "👑 Приоритетная поддержка",
    ],
}

router = Router()

_TON_RATE = 3.0  # 1 TON ≈ $3


def _ton_wallet() -> str:
    return os.getenv("TON_WALLET", "")


def _tron_wallet() -> str:
    return os.getenv("TRON_WALLET", "")


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


async def _build_menu_text_and_kb(pool: asyncpg.Pool, user_id: int):
    plan = await get_plan(pool, user_id)
    lim = BOT_LIMITS.get(plan, 3)
    lim_label = "∞" if lim >= 9999 else str(lim)
    emoji = PLAN_EMOJIS.get(plan, "🆓")
    text = (
        f"💳 <b>Подписка</b>\n\n"
        f"Текущий план: <b>{emoji} {plan.upper()}</b> · до {lim_label} ботов\n\n"
        f"Выберите план для апгрейда:\n\n"
        f"⭐ <b>STARTER</b> — $9/мес · до 10 ботов\n"
        f"<i>Inbox, CRM, расписание, воронки, диплинки, SEO</i>\n\n"
        f"🚀 <b>PRO</b> — $25/мес · до 30 ботов\n"
        f"<i>A/B тесты, аналитика активности, мультигео, массовые операции</i>\n\n"
        f"👑 <b>ENTERPRISE</b> — $69/мес · без ограничений\n"
        f"<i>Swarm, кластеры, сетевая рассылка v2, AI-ассистент, приоритетная поддержка</i>\n\n"
        f"💡 Нажмите на план → выберите период → выберите криптовалюту → оплатите\n"
        f"❓ Кнопка «Что входит в план» — полный список функций"
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
        kb.button(
            text="❓ Что входит в план",
            callback_data=SubCb(action="plan_features", plan=p),
        )
    kb.adjust(2)
    return text, kb.as_markup()


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, pool: asyncpg.Pool) -> None:
    text, markup = await _build_menu_text_and_kb(pool, message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(SubCb.filter(F.action == "menu"))
async def cb_sub_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    text, markup = await _build_menu_text_and_kb(pool, callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(SubCb.filter(F.action == "plan_features"))
async def cb_plan_features(callback: CallbackQuery, callback_data: SubCb) -> None:
    await callback.answer()
    plan = callback_data.plan
    if plan not in PLAN_DETAILED_FEATURES:
        await callback.answer("Неизвестный план.", show_alert=True)
        return

    em = PLAN_EMOJIS.get(plan, "")
    price = PLAN_PRICES_USD.get(plan, 0)
    features = PLAN_DETAILED_FEATURES[plan]
    bot_limit = BOT_LIMITS.get(plan, 0)
    limit_label = "∞" if bot_limit >= 9999 else str(bot_limit)

    features_text = "\n".join(f"  {f}" for f in features)

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"💳 Оформить {plan.upper()}",
        callback_data=SubCb(action="choose_plan", plan=plan),
    )
    kb.button(text="◀️ Назад к планам", callback_data=SubCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"{em} <b>{plan.upper()}</b> — ${price}/мес · до {limit_label} ботов\n\n"
        f"<b>Что входит в план:</b>\n\n"
        f"{features_text}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


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
        f"💳 {em} <b>{plan.upper()}</b>\n"
        f"Базовая цена: <b>${base}/мес</b>\n\n"
        f"📅 Выберите период (чем дольше — тем дешевле):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SubCb.filter(F.action == "choose_period"))
async def cb_choose_period(callback: CallbackQuery, callback_data: SubCb) -> None:
    await callback.answer()
    plan, months = callback_data.plan, callback_data.months
    ton = _ton_wallet()
    tron = _tron_wallet()
    kb = InlineKeyboardBuilder()
    if ton:
        kb.button(
            text="💎 TON",
            callback_data=SubCb(action="pay", plan=plan, months=months, currency="TON"),
        )
    if tron:
        kb.button(
            text="💵 USDT (TRC-20)",
            callback_data=SubCb(action="pay", plan=plan, months=months, currency="USDT_TRC20"),
        )
    if not ton and not tron:
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
    wallet = _ton_wallet() if currency == "TON" else _tron_wallet()
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
