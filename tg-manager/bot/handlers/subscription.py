"""Subscription plan selection and crypto payment flow."""

from __future__ import annotations
import logging
import os
import random
import string
import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import SubCb
from bot.states import PaymentSettingsFSM
from bot.utils.subscription import get_plan, PLAN_EMOJIS, BOT_LIMITS, is_platform_admin
from config import PLAN_PRICES_USD, PERIOD_DISCOUNTS
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

PLAN_DETAILED_FEATURES: dict[str, list[str]] = {
    "free": [
        "🤖 До 3 ботов",
        "📢 Рассылки (без ограничений)",
        "↩️ Авто-ответы по ключевым словам",
        "⏰ Расписание рассылок",
        "📨 Inbox — live-чат с пользователями",
        "🤖 Команды бота",
        "📝 Шаблоны сообщений",
        "🌐 Вебхуки",
        "📊 Базовая статистика",
    ],
    "starter": [
        "🤖 До 10 ботов",
        "✅ Всё из FREE +",
        "🏷 CRM и теги (сегментация аудитории)",
        "🤖 Автоматизация (правила и триггеры)",
        "🔗 Цепочки сообщений (воронки)",
        "🔗 Диплинки с аналитикой",
        "📈 SEO-анализ профиля бота",
        "👥 Экспорт аудитории (CSV)",
        "🏆 Трекер позиций в поиске Telegram",
        "📊 Отчёты по видимости и позициям",
        "📱 1 личный Telegram-аккаунт",
    ],
    "pro": [
        "🤖 До 30 ботов",
        "✅ Всё из STARTER +",
        "📺 Фабрика каналов (создание, управление, публикация)",
        "👥 Фабрика групп",
        "🔍 Парсер аудитории из каналов и групп",
        "🌐 Массовые операции (bulk join/leave/edit)",
        "🧪 A/B тесты сообщений",
        "🎯 Аналитика активности (горячие/холодные/потерянные)",
        "🌍 Мультигео (имена/описания по языку)",
        "📱 До 3 личных Telegram-аккаунтов",
    ],
    "enterprise": [
        "🤖 ∞ ботов — без ограничений",
        "✅ Всё из PRO + эксклюзивные функции:",
        "📱 ∞ личных Telegram-аккаунтов",
        "🤖 AI-ассистент для анализа и генерации контента",
        "📩 DM-кампании (личные сообщения в масштабе)",
        "🌐 Global Presence (массовое присутствие)",
        "📡 Сетевые операции и аналитика сети ботов",
        "📢 Сетевые рассылки (несколько ботов одновременно)",
        "🧬 Swarm — умный роутинг и балансировка нагрузки",
        "🌐 Кластеры ботов и управление сетью",
        "📢 Сетевая рассылка v2 (дедупликация, сегментация)",
        "📊 Поведенческая аналитика и метрики вовлечённости",
        "👑 Приоритетная поддержка 24/7",
    ],
}

_PAY_SETTING_LABELS: dict[str, str] = {
    "TON_WALLET": "💎 TON кошелёк",
    "TRON_WALLET": "💵 USDT (TRC-20) кошелёк",
    "TON_API_KEY": "🔑 TON API ключ",
    "TON_RATE": "📊 Курс TON/USD",
}

router = Router()


# ── helpers ──────────────────────────────────────────────────────────────────


def _ton_wallet() -> str:
    return os.getenv("TON_WALLET", "")


def _tron_wallet() -> str:
    return os.getenv("TRON_WALLET", "")


def _get_ton_rate() -> float:
    try:
        return float(os.getenv("TON_RATE", "3.0") or "3.0")
    except ValueError:
        return 3.0


def _gen_ref() -> str:
    return "PAY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _calc(plan: str, months: int, currency: str) -> tuple[float, float]:
    """Returns (usd_total, crypto_amount)."""
    base = PLAN_PRICES_USD.get(plan, 0)
    disc = PERIOD_DISCOUNTS.get(months, 0)
    usd = round(base * months * (1 - disc / 100), 2)
    if currency == "TON":
        return usd, round(usd / _get_ton_rate(), 2)
    return usd, usd  # USDT 1:1


def _mask(val: str) -> str:
    if not val:
        return "❌ не задан"
    if len(val) > 10:
        return f"✅ {val[:5]}...{val[-4:]}"
    return f"✅ {val}"


# ── subscription menu ────────────────────────────────────────────────────────


async def _get_plan_expiry(pool: asyncpg.Pool, user_id: int):
    """Возвращает (plan, expires_at) или (plan, None) для бесплатного плана."""
    from bot.utils.subscription import is_platform_admin
    if is_platform_admin(user_id):
        return "enterprise", None
    row = await pool.fetchrow(
        "SELECT plan, expires_at FROM subscriptions "
        "WHERE user_id=$1 AND is_active=true AND expires_at > now()",
        user_id,
    )
    if row:
        return row["plan"], row["expires_at"]
    return "free", None


async def _build_menu_text_and_kb(pool: asyncpg.Pool, user_id: int):
    from datetime import datetime, timezone
    plan, expires_at = await _get_plan_expiry(pool, user_id)
    lim = BOT_LIMITS.get(plan, 3)
    lim_label = "∞" if lim >= 9999 else str(lim)
    emoji = PLAN_EMOJIS.get(plan, "🆓")
    ton_ok = "✅" if _ton_wallet() else "❌"
    tron_ok = "✅" if _tron_wallet() else "❌"
    pay_status = f"TON {ton_ok}  USDT {tron_ok}"

    # Блок информации о текущем плане
    if plan == "free":
        plan_info = f"Текущий план: <b>{emoji} FREE</b> · до {lim_label} ботов"
    else:
        from bot.utils.subscription import is_platform_admin
        if is_platform_admin(user_id):
            plan_info = f"Текущий план: <b>{emoji} {plan.upper()}</b> · ∞ ботов\n🔑 <i>Администратор платформы</i>"
        elif expires_at:
            now_utc = datetime.now(timezone.utc)
            if expires_at.tzinfo is None:
                from datetime import timezone as tz
                expires_utc = expires_at.replace(tzinfo=tz.utc)
            else:
                expires_utc = expires_at
            days_left = (expires_utc - now_utc).days
            expire_str = expires_utc.strftime("%d.%m.%Y")
            if days_left <= 3:
                days_badge = f"⚠️ <b>Осталось {days_left} дн.</b> — продлите до {expire_str}"
            elif days_left <= 14:
                days_badge = f"⏳ Осталось <b>{days_left} дн.</b> (до {expire_str})"
            else:
                days_badge = f"✅ Активен до <b>{expire_str}</b> ({days_left} дн.)"
            plan_info = f"Текущий план: <b>{emoji} {plan.upper()}</b> · до {lim_label} ботов\n{days_badge}"
        else:
            plan_info = f"Текущий план: <b>{emoji} {plan.upper()}</b> · до {lim_label} ботов"

    text = (
        f"💳 <b>Подписка</b>\n\n"
        f"{plan_info}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⭐ <b>STARTER</b> — $9/мес · 10 ботов\n"
        f"<i>CRM, воронки, диплинки, SEO, трекер позиций, 1 аккаунт</i>\n\n"
        f"🚀 <b>PRO</b> — $25/мес · 30 ботов\n"
        f"<i>Фабрики каналов/групп, парсер, A/B тесты, мультигео, 3 аккаунта</i>\n\n"
        f"👑 <b>ENTERPRISE</b> — $69/мес · ∞ без лимитов\n"
        f"⭐ <b>ЛУЧШИЙ ВЫБОР — весь топовый функционал</b>\n"
        f"<i>AI-ассистент, DM-кампании, Global Presence, сетевые операции,\n"
        f"Swarm, кластеры, поведенческая аналитика, ∞ ботов и аккаунтов</i>\n"
        f"<i>При годовой оплате: экономия $165/год (скидка 20%)</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💡 Выберите план → период → оплатите\n"
        f"<i>Оплата: {pay_status}</i>"
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
            text="❓ Что входит",
            callback_data=SubCb(action="plan_features", plan=p),
        )
    from bot.callbacks import BotCb

    if is_platform_admin(user_id):
        kb.button(
            text="⚙️ Настройка оплаты", callback_data=SubCb(action="payment_settings")
        )
        kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
        kb.adjust(2, 2, 2, 2)
    else:
        kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
        kb.adjust(2, 2, 2, 1)
    return text, kb.as_markup()


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, pool: asyncpg.Pool) -> None:
    text, markup = await _build_menu_text_and_kb(pool, message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(SubCb.filter(F.action == "menu"))
async def cb_sub_menu(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _build_menu_text_and_kb(pool, callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


# ── plan features ────────────────────────────────────────────────────────────


_PLAN_HIGHLIGHTS: dict[str, str] = {
    "starter": (
        "Идеально для начинающих: управляйте несколькими ботами, "
        "сегментируйте аудиторию по тегам, автоматизируйте общение через воронки "
        "и отслеживайте позиции в поиске Telegram."
    ),
    "pro": (
        "Для растущего бизнеса: создавайте каналы и группы прямо из бота, "
        "парсите аудиторию конкурентов, запускайте A/B тесты и управляйте "
        "несколькими Telegram-аккаунтами из одного интерфейса."
    ),
    "enterprise": (
        "Максимум возможностей: AI-ассистент для анализа и управления, "
        "неограниченное число ботов и аккаунтов, DM-кампании в масштабе, "
        "поведенческая аналитика, Swarm-роутинг и глобальное присутствие."
    ),
}

_PLAN_ANNUAL_SAVINGS: dict[str, str] = {
    "starter": "При оплате на год: экономия $21.6 (скидка 20%)",
    "pro": "При оплате на год: экономия $60 (скидка 20%)",
    "enterprise": "При оплате на год: экономия $165.6 (скидка 20%)",
}


@router.callback_query(SubCb.filter(F.action == "plan_features"))
async def cb_plan_features(callback: CallbackQuery, callback_data: SubCb, pool: asyncpg.Pool) -> None:
    plan = callback_data.plan or ""
    if plan not in PLAN_DETAILED_FEATURES:
        await callback.answer("Неизвестный план.", show_alert=True)
        return
    await callback.answer()
    em = PLAN_EMOJIS.get(plan, "")
    price = PLAN_PRICES_USD.get(plan, 0)
    bot_limit = BOT_LIMITS.get(plan, 0)
    limit_label = "∞" if bot_limit >= 9999 else str(bot_limit)
    features_text = "\n".join(f"  {f}" for f in PLAN_DETAILED_FEATURES[plan])
    highlight = _PLAN_HIGHLIGHTS.get(plan, "")
    savings = _PLAN_ANNUAL_SAVINGS.get(plan, "")

    # Показываем текущий план пользователя
    current_plan = await get_plan(pool, callback.from_user.id)
    is_current = current_plan == plan
    status_line = "✅ <i>Это ваш текущий план</i>\n\n" if is_current else ""

    kb = InlineKeyboardBuilder()
    if not is_current:
        kb.button(
            text=f"💳 Оформить {plan.upper()} — ${price}/мес",
            callback_data=SubCb(action="choose_plan", plan=plan),
        )
    kb.button(text="◀️ Назад к планам", callback_data=SubCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"{em} <b>{plan.upper()}</b> — ${price}/мес · до {limit_label} ботов\n\n"
        f"{status_line}"
        f"<i>{highlight}</i>\n\n"
        f"<b>Что входит в план:</b>\n\n{features_text}\n\n"
        f"<i>💰 {savings}</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── choose plan / period ─────────────────────────────────────────────────────


@router.callback_query(SubCb.filter(F.action == "choose_plan"))
async def cb_choose_plan(callback: CallbackQuery, callback_data: SubCb) -> None:
    plan = callback_data.plan or ""
    if plan not in PLAN_PRICES_USD:
        await callback.answer("Неизвестный план.", show_alert=True)
        return
    await callback.answer()
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
        f"💳 {em} <b>{plan.upper()}</b>\nБазовая цена: <b>${base}/мес</b>\n\n"
        f"📅 Выберите период (чем дольше — тем дешевле):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SubCb.filter(F.action == "choose_period"))
async def cb_choose_period(
    callback: CallbackQuery, callback_data: SubCb, pool: asyncpg.Pool
) -> None:
    plan, months = callback_data.plan or "", callback_data.months
    ton = _ton_wallet()
    tron = _tron_wallet()
    await callback.answer()

    if not ton and not tron:
        # No wallets configured
        usd, _ = _calc(plan, months, "TON")
        kb = InlineKeyboardBuilder()
        if is_platform_admin(callback.from_user.id):
            kb.button(
                text="🎁 Активировать себе (Admin)",
                callback_data=SubCb(action="admin_grant", plan=plan, months=months),
            )
            kb.button(
                text="⚙️ Настроить кошельки",
                callback_data=SubCb(action="payment_settings"),
            )
        else:
            kb.button(
                text="📩 Запросить подписку",
                callback_data=SubCb(action="request_sub", plan=plan, months=months),
            )
        kb.button(text="◀️ Назад", callback_data=SubCb(action="choose_plan", plan=plan))
        kb.adjust(1)
        await callback.message.edit_text(
            f"💳 <b>{PLAN_EMOJIS.get(plan, '')} {plan.upper()} × {months} мес.</b> — <b>${usd}</b>\n\n"
            f"⚠️ Автоматическая оплата не настроена.\n\n"
            f"Нажмите <b>«📩 Запросить подписку»</b> — администратор активирует вручную после оплаты.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Wallets configured — show currency selection
    kb = InlineKeyboardBuilder()
    if ton:
        usd_ton, crypto_ton = _calc(plan, months, "TON")
        kb.button(
            text=f"💎 TON — {crypto_ton:.2f} TON (≈${usd_ton})",
            callback_data=SubCb(action="pay", plan=plan, months=months, currency="TON"),
        )
    if tron:
        usd_usdt, _ = _calc(plan, months, "USDT_TRC20")
        kb.button(
            text=f"💵 USDT TRC-20 — {usd_usdt:.2f} USDT",
            callback_data=SubCb(
                action="pay", plan=plan, months=months, currency="USDT_TRC20"
            ),
        )
    kb.button(text="◀️ Назад", callback_data=SubCb(action="choose_plan", plan=plan))
    kb.adjust(1)
    usd_show, _ = _calc(plan, months, "TON" if ton else "USDT_TRC20")
    await callback.message.edit_text(
        f"💳 <b>{PLAN_EMOJIS.get(plan, '')} {plan.upper()} × {months} мес.</b>\n\n"
        f"Итого: <b>${usd_show}</b>\n\nВыберите способ оплаты:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── payment ──────────────────────────────────────────────────────────────────


@router.callback_query(SubCb.filter(F.action == "pay"))
async def cb_pay(
    callback: CallbackQuery, callback_data: SubCb, pool: asyncpg.Pool
) -> None:
    plan, months, currency = (
        callback_data.plan or "",
        callback_data.months,
        callback_data.currency or "",
    )
    wallet = _ton_wallet() if currency == "TON" else _tron_wallet()
    if not wallet:
        await callback.answer(
            "Кошелёк не настроен. Обратитесь к администратору.", show_alert=True
        )
        return
    await callback.answer()

    usd, crypto = _calc(plan, months, currency)

    ref = _gen_ref()
    for _ in range(5):
        if not await pool.fetchrow("SELECT id FROM payments WHERE reference=$1", ref):
            break
        ref = _gen_ref()

    await pool.execute(
        """INSERT INTO payments (user_id, plan, period_months, currency, amount_crypto, amount_usd,
                                  wallet_address, reference)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (reference) DO NOTHING""",
        callback.from_user.id,
        plan,
        months,
        currency,
        crypto,
        usd,
        wallet,
        ref,
    )

    if currency == "TON":
        crypto_str = f"{crypto:.2f} TON"
        note = (
            f"⚠️ <b>Укажите комментарий к переводу (обязательно):</b>\n"
            f"<code>{ref}</code>\n\n"
            "Без комментария подписка не активируется автоматически."
        )
    else:
        crypto_str = f"{crypto:.2f} USDT"
        note = (
            f"Переведите ровно <b>{crypto:.2f} USDT</b>.\n"
            "Сеть: <b>TRC-20 (TRON)</b>. Другие сети не принимаются."
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Проверить статус", callback_data=SubCb(action="check_status"))
    kb.button(text="◀️ Назад к планам", callback_data=SubCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"💳 <b>Оплата {plan.upper()} на {months} мес.</b>\n\n"
        f"Сумма: <b>{crypto_str}</b> (≈ ${usd})\n"
        f"Кошелёк: <code>{wallet}</code>\n\n"
        f"{note}\n\n"
        f"⏱ Ожидание подтверждения: до 30 минут\n"
        f"Подписка активируется автоматически.\n\n"
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
        "confirming": "🔄 Подтверждается в блокчейне...",
        "confirmed": "✅ Подтверждён — подписка активирована!",
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
        f"План: <b>{row['plan'].upper()}</b> на {row['period_months']} мес.\n"
        f"Сумма: {row['amount_crypto']} {row['currency']}\n"
        f"Референс: <code>{row['reference']}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── request subscription (no wallets configured) ─────────────────────────────


@router.callback_query(SubCb.filter(F.action == "request_sub"))
async def cb_request_sub(callback: CallbackQuery, callback_data: SubCb) -> None:
    await callback.answer()
    plan, months = callback_data.plan or "", callback_data.months
    usd, _ = _calc(plan, months, "TON")
    em = PLAN_EMOJIS.get(plan, "")
    uid = callback.from_user.id
    user_label = (
        f"@{callback.from_user.username}"
        if callback.from_user.username
        else callback.from_user.first_name or str(uid)
    )

    admin_ids = [
        int(x.strip())
        for x in os.getenv("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    ]
    notify = (
        f"💳 <b>Запрос на подписку</b>\n\n"
        f"Пользователь: {user_label} (<code>{uid}</code>)\n"
        f"План: <b>{em} {plan.upper()}</b> × {months} мес.\n"
        f"Сумма: <b>${usd}</b>\n\n"
        f"Чтобы активировать после получения оплаты:\n"
        f"<b>/admin</b> → 💰 Выдать подписку →\n"
        f"<code>{uid} {plan} {months}</code>"
    )
    for admin_id in admin_ids:
        try:
            await callback.bot.send_message(admin_id, notify, parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "Ошибка отправки уведомления администратору о запросе подписки")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К планам", callback_data=SubCb(action="menu"))
    await callback.message.edit_text(
        f"✅ <b>Запрос отправлен!</b>\n\n"
        f"Администратор получил уведомление о вашем запросе:\n"
        f"<b>{em} {plan.upper()}</b> × {months} мес. — <b>${usd}</b>\n\n"
        f"Подписка будет активирована после подтверждения оплаты.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── admin grant (self) ────────────────────────────────────────────────────────


@router.callback_query(SubCb.filter(F.action == "admin_grant"))
async def cb_admin_grant(
    callback: CallbackQuery, callback_data: SubCb, pool: asyncpg.Pool
) -> None:
    if not is_platform_admin(callback.from_user.id):
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    await callback.answer()
    plan, months = callback_data.plan or "", max(1, callback_data.months)
    from datetime import datetime, timedelta, timezone

    await pool.execute(
        """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
           VALUES($1, $2, now() + ($3 || ' months')::INTERVAL, true)
           ON CONFLICT(user_id) DO UPDATE
           SET plan      = EXCLUDED.plan,
               is_active = true,
               expires_at = CASE
                   WHEN subscriptions.expires_at > now()
                       THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
                   ELSE now() + ($3 || ' months')::INTERVAL
               END""",
        callback.from_user.id, plan, str(months),
    )
    row = await pool.fetchrow(
        "SELECT expires_at FROM subscriptions WHERE user_id=$1", callback.from_user.id
    )
    expires = row["expires_at"] if row else (datetime.now(timezone.utc) + timedelta(days=30 * months))
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=SubCb(action="menu"))
    await callback.message.edit_text(
        f"✅ <b>Подписка активирована!</b>\n\n"
        f"План: <b>{plan.upper()}</b>\n"
        f"Срок: {months} мес. (до {expires.strftime('%d.%m.%Y')})",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── payment settings (admin) ─────────────────────────────────────────────────


def _payment_settings_kb() -> object:
    kb = InlineKeyboardBuilder()
    for key, label in _PAY_SETTING_LABELS.items():
        val = os.getenv(key, "")
        kb.button(
            text=f"{label}: {_mask(val)}",
            callback_data=SubCb(action="pay_edit", plan=key),
        )
    kb.button(text="◀️ Назад к подписке", callback_data=SubCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def _payment_settings_text() -> str:
    ton_ok = bool(_ton_wallet())
    tron_ok = bool(_tron_wallet())
    key_ok = bool(os.getenv("TON_API_KEY", ""))
    rate = _get_ton_rate()

    status_lines = [
        f"{'✅' if ton_ok else '❌'} TON кошелёк: {_mask(_ton_wallet())}",
        f"{'✅' if tron_ok else '❌'} USDT (TRC-20): {_mask(os.getenv('TRON_WALLET', ''))}",
        f"{'✅' if key_ok else '⚠️'} TON API ключ: {_mask(os.getenv('TON_API_KEY', ''))}",
        f"📊 Курс TON/USD: <b>${rate:.2f}</b>",
    ]
    pay_ok = ton_ok or tron_ok
    return (
        "⚙️ <b>Настройка оплаты</b>\n\n"
        + "\n".join(status_lines)
        + "\n\n"
        + (
            "✅ Автооплата активна — пользователи могут платить самостоятельно.\n\n"
            if pay_ok
            else "❌ Кошельки не настроены — пользователи не могут оплатить автоматически.\n\n"
        )
        + "<b>Инструкция:</b>\n"
        "1. Задайте TON или USDT кошелёк\n"
        "2. TON API ключ (необязательно): получить на tonconsole.com\n"
        "3. Курс TON/USD: обновляйте раз в неделю\n\n"
        "Нажмите на поле чтобы изменить."
    )


@router.callback_query(SubCb.filter(F.action == "payment_settings"))
async def cb_payment_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_platform_admin(callback.from_user.id):
        await callback.answer("⛔️ Только для администратора.", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        _payment_settings_text(),
        parse_mode="HTML",
        reply_markup=_payment_settings_kb(),
    )


@router.callback_query(SubCb.filter(F.action == "pay_edit"))
async def cb_pay_edit(
    callback: CallbackQuery, callback_data: SubCb, state: FSMContext
) -> None:
    if not is_platform_admin(callback.from_user.id):
        await callback.answer("⛔️", show_alert=True)
        return
    key = callback_data.plan or ""  # reused field for the setting key
    if key not in _PAY_SETTING_LABELS:
        await callback.answer("Неизвестный параметр.", show_alert=True)
        return
    await callback.answer()

    label = _PAY_SETTING_LABELS[key]
    cur = os.getenv(key, "")
    masked = _mask(cur)

    hints = {
        "TON_WALLET": "Пример: <code>UQD...abc</code> (адрес TON-кошелька)",
        "TRON_WALLET": "Пример: <code>TXyz...abc</code> (адрес TRC-20 кошелька)",
        "TON_API_KEY": "Получить на tonconsole.com → API Keys",
        "TON_RATE": "Текущий курс TON/USD. Пример: <code>5.50</code>",
    }

    await state.set_state(PaymentSettingsFSM.waiting_value)
    await state.update_data(key=key)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=SubCb(action="payment_settings"))
    await callback.message.edit_text(
        f"✏️ <b>{label}</b>\n\n"
        f"Текущее значение: {masked}\n\n"
        f"{hints.get(key, '')}\n\n"
        "Отправьте новое значение следующим сообщением.\n"
        "Или нажмите Отмена.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(PaymentSettingsFSM.waiting_value, F.text)
async def msg_payment_setting_value(
    message: Message, state: FSMContext, http: aiohttp.ClientSession
) -> None:
    data = await state.get_data()
    key = data.get("key", "")
    if key not in _PAY_SETTING_LABELS:
        await state.clear()
        return

    value = message.text.strip()

    if key == "TON_RATE":
        try:
            rate = float(value)
            if rate <= 0:
                raise ValueError
            value = f"{rate:.4f}"
        except ValueError:
            kb = InlineKeyboardBuilder()
            kb.button(text="❌ Отмена", callback_data=SubCb(action="payment_settings"))
            await message.answer(
                "❌ Введите положительное число. Пример: <code>5.50</code>\n\n"
                "Попробуйте снова или нажмите Отмена.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
            return

    if key in ("TON_WALLET", "TRON_WALLET") and len(value) < 10:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=SubCb(action="payment_settings"))
        await message.answer(
            "❌ Адрес кошелька слишком короткий. Проверьте и отправьте снова.",
            reply_markup=kb.as_markup(),
        )
        return

    await state.clear()

    # Apply immediately in process
    os.environ[key] = value

    # Save to Railway if configured
    railway_saved = False
    try:
        from services import railway_api

        if railway_api.is_configured():
            await railway_api.set_variable(http, key, value)
            railway_saved = True
    except Exception:
        log_exc_swallow(log, "Ошибка сохранения платёжной настройки в Railway API")

    label = _PAY_SETTING_LABELS[key]
    note = (
        ""
        if railway_saved
        else "\n\n⚠️ Railway API не настроен — значение активно до перезапуска бота. Настройте Railway Token в /admin для постоянного сохранения."
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚙️ К настройкам оплаты", callback_data=SubCb(action="payment_settings")
    )
    kb.button(text="💳 К подписке", callback_data=SubCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>{label}</b> обновлён{note}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
