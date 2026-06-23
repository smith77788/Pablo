import asyncio
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
import asyncpg
from bot.keyboards import main_menu
from bot.utils.subscription import get_plan, PLAN_EMOJIS, is_platform_admin
from database import db
from bot.callbacks import (
    BotCb,
    BmCb,
    AccCb,
    ChanCb,
    GroupFCb,
    ProxyCb,
    VisCb,
    PromoCb,
    CompCb,
    ChanFactCb,
    EcoCb,
    ClustMCb,
    WarmupCb,
    ParserCb,
    HealthCb,
    MassOpCb,
    MassPubCb,
    AssetTplCb,
    SubCb,
    StrikeCb,
    GeoPresenceCb,
    AiCb,
    WorkspaceCb,
    RefCb,
    TopoCb,
    RegCb,
    DmCb,
    QuickPostCb,
)
from bot.handlers.admin import notify_new_platform_user
from services.logger import log_exc_swallow
import logging

log = logging.getLogger(__name__)

router = Router()


async def _record_reentry_safe(pool, uid: int, days_absent: float) -> None:
    try:
        from services import behavioral_engine

        await behavioral_engine.record_reentry(pool, uid, "platform", uid, days_absent)
    except Exception as e:
        log.debug("record_reentry failed: %s", e)


BUILD_VERSION = "2026.06.06-r36"


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    await message.answer(
        f"🔖 <b>BotMother OS</b> build <code>{BUILD_VERSION}</code>", parse_mode="HTML"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Нет активного действия для отмены.",
            reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
        )
        return
    await state.clear()
    await message.answer(
        "❌ Действие отменено.",
        reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    admin = is_platform_admin(uid)

    # Parallel: blocked check + user info + bots list (all independent reads)
    try:
        blocked_val, existing, bots = await asyncio.gather(
            pool.fetchval("SELECT 1 FROM blocked_users WHERE user_id=$1", uid),
            db.get_user_info(pool, uid),
            db.get_bots(pool, uid),
            return_exceptions=True,
        )
    except Exception:
        blocked_val, existing, bots = None, None, []

    if isinstance(blocked_val, BaseException):
        log_exc_swallow(log, "Не удалось проверить блокировку пользователя")
        blocked_val = None
    if isinstance(existing, BaseException):
        existing = None
    if isinstance(bots, BaseException):
        bots = []

    if blocked_val:
        await message.answer("⛔️ Ваш аккаунт заблокирован. Обратитесь в поддержку.")
        return

    is_new = False
    try:
        is_new = existing is None
        await db.register_or_update_user(
            pool,
            uid,
            message.from_user.username,
            message.from_user.first_name or "",
        )
        if is_new:
            await notify_new_platform_user(
                message.bot,
                pool,
                uid,
                message.from_user.username,
                message.from_user.first_name or "",
            )
        elif existing and (existing.get("last_seen") or existing.get("last_active")):
            # Record reentry if user was absent 7+ days
            last = existing.get("last_seen") or existing.get("last_active")
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days_absent = (datetime.now(timezone.utc) - last).total_seconds() / 86400
            if days_absent >= 7:
                asyncio.create_task(_record_reentry_safe(pool, uid, days_absent))
    except Exception:
        log_exc_swallow(log, "Не удалось зарегистрировать или обновить пользователя")

    # Handle referral code from /start inv_XXXXXX
    if is_new:
        args = message.text.split(maxsplit=1)
        start_param = args[1].strip() if len(args) > 1 else ""
        if start_param.startswith("inv_"):
            try:
                referrer_id = await db.get_user_by_referral_code(pool, start_param)
                if referrer_id and referrer_id != uid:
                    recorded = await db.record_platform_referral(pool, referrer_id, uid)
                    if recorded:
                        await db.give_welcome_bonus(pool, uid, message.bot)
            except Exception as e:
                log.warning("Referral processing error: %s", e)

    # bots already fetched in parallel above
    bot_count = len(bots)

    if not bot_count:
        await message.answer(
            "👋 <b>Добро пожаловать в BotMother!</b>\n\n"
            "Это система управления Telegram-активами:\n"
            "боты, каналы, группы, аккаунты — всё в одном месте.\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🚀 <b>Быстрый старт — выберите сценарий:</b>\n\n"
            "🤖 <b>Хочу управлять ботом</b>\n"
            "→ ➕ Добавить бота → вставить токен от @BotFather\n"
            "→ Рассылки, аудитория, авто-ответы, CRM\n\n"
            "📡 <b>Хочу управлять каналами</b>\n"
            "→ /menu → 📱 Аккаунты & Боты → 📡 Каналы\n"
            "→ Импорт, создание, публикация во все каналы\n\n"
            "📱 <b>Хочу операции через аккаунт</b>\n"
            "→ /menu → 📱 Аккаунты & Боты → 📱 TG-аккаунты\n"
            "→ Создание каналов/групп, вступление, публикация\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>Полное меню OS:</b> /menu\n"
            "❓ <b>Помощь:</b> /help\n\n"
            f"<i>ID: <code>{uid}</code></i>",
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=admin),
        )
        return

    total_aud = sum(b["audience_count"] for b in bots if "audience_count" in b.keys())

    active_broadcasts = 0
    try:
        bot_ids = [b["bot_id"] for b in bots]
        active_broadcasts = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM broadcasts WHERE bot_id = ANY($1::bigint[]) AND status IN ('pending', 'running')",
                bot_ids,
            )
            or 0
        )
    except Exception:
        log_exc_swallow(log, "Не удалось получить количество активных рассылок")
        active_broadcasts = 0

    stats_lines = [
        f"🤖 Ботов: <b>{bot_count}</b>",
        f"👥 Аудитория: <b>{total_aud}</b> чел.",
    ]
    if active_broadcasts:
        stats_lines.append(f"📢 Активных рассылок: <b>{active_broadcasts}</b>")
    summary = " · ".join(stats_lines[:2])
    extra = f"\n{stats_lines[2]}" if active_broadcasts else ""

    await message.answer(
        f"👋 <b>BotMother OS</b>  <code>v{BUILD_VERSION}</code>\n\n"
        f"{summary}{extra}\n\n"
        f"ID: <code>{uid}</code>\n\n"
        f"💡 Нажмите на бота из списка → откроется меню управления",
        parse_mode="HTML",
        reply_markup=main_menu(is_admin=admin),
    )


@router.callback_query(BotCb.filter(F.action == "help"))
async def cb_help(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    try:
        plan = await get_plan(pool, uid)
    except Exception:
        log_exc_swallow(log, "Не удалось получить план пользователя для /help callback")
        plan = "free"
    emoji = PLAN_EMOJIS.get(plan, "🆓")

    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))

    text = (
        f"❓ <b>Справка BotMother OS</b>\n\n"
        f"Ваш план: <b>{emoji} {plan.upper()}</b>\n\n"
        f"<b>📋 Команды:</b>\n"
        f"/start — главное меню\n"
        f"/subscription — подписка и оплата\n"
        f"/ranking — трекер позиций в поиске\n"
        f"/accounts — мои Telegram-аккаунты\n"
        f"/ops — операции и отчёты\n"
        f"/report — последние отчёты по операциям\n"
        f"/stats — статистика инфраструктуры\n"
        f"/cancel — отменить текущее действие\n\n"
        f"<b>🤖 Разделы бота (открываются из меню бота):</b>\n"
        f"• Аудитория — список пользователей\n"
        f"• Рассылка — сообщение всем\n"
        f"• Команды, Шаблоны, Авто-ответы\n"
        f"• Inbox — живой чат (💎 подписка)\n"
        f"• Цепочки — воронки (💎 подписка)\n"
        f"• CRM, SEO, Диплинки (💎 подписка)\n"
        f"• A/B тесты, Активность (💎 подписка)\n"
        f"• 📊 Позиции в поиске (💎 подписка)\n\n"
        f"<b>🌐 Сеть &amp; операции</b> — управление всеми ботами сразу\n"
        f"<b>📡 Операции с аккаунтами</b> — через личный Telegram-аккаунт"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.message(Command("help"))
async def cmd_help(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    admin = is_platform_admin(uid)
    try:
        plan = await get_plan(pool, uid)
    except Exception:
        log_exc_swallow(log, "Не удалось получить план пользователя для /help команды")
        plan = "free"
    emoji = PLAN_EMOJIS.get(plan, "🆓")

    text = (
        f"❓ <b>Справка BotMother OS</b>\n\n"
        f"Ваш план: <b>{emoji} {plan.upper()}</b>\n\n"
        f"<b>📋 Команды:</b>\n"
        f"/start — главное меню\n"
        f"/subscription — подписка и оплата\n"
        f"/ranking — трекер позиций в поиске\n"
        f"/accounts — мои Telegram-аккаунты\n"
        f"/ops — операции и отчёты\n"
        f"/report — последние отчёты по операциям\n"
        f"/stats — статистика инфраструктуры\n"
        f"/cancel — отменить текущее действие\n\n"
        f"<b>🤖 Разделы бота:</b>\n"
        f"Добавьте бота → выберите из списка → откроется меню:\n"
        f"• Аудитория, Рассылка, Команды, Шаблоны, Авто-ответы\n"
        f"• Inbox, Цепочки, CRM, SEO, Диплинки (💎 подписка)\n"
        f"• A/B тесты, Активность, Мультигео (💎 подписка)\n"
        f"• 📊 Позиции в поиске Telegram (💎 подписка)\n\n"
        f"<b>🌐 Сеть &amp; операции</b> — управление всеми ботами сразу\n"
        f"<b>📡 Операции с аккаунтами</b> — через личный Telegram-аккаунт\n\n"
        f"💡 Все функции с замком 🔒 открываются через /subscription"
    )
    await message.answer(
        text, parse_mode="HTML", reply_markup=main_menu(is_admin=admin)
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, pool: asyncpg.Pool) -> None:
    """Show a real-data analytics summary: accounts, operations, queue, errors."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import InfraCb, MassOpCb, BmCb

    uid = message.from_user.id

    # Accounts
    try:
        acc_total = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", uid
        ) or 0
    except Exception:
        acc_total = 0

    try:
        acc_banned = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND acc_status IN ('banned','spamblock','deactivated')", uid
        ) or 0
    except Exception:
        acc_banned = 0

    try:
        acc_cooldown = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND cooldown_until > NOW()", uid
        ) or 0
    except Exception:
        acc_cooldown = 0

    # Operations stats
    try:
        op_stats = await pool.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE status='running')  AS running,
                   COUNT(*) FILTER (WHERE status='pending')  AS pending,
                   COUNT(*) FILTER (WHERE status='done'
                       AND finished_at > NOW() - INTERVAL '24h') AS done_24h,
                   COUNT(*) FILTER (WHERE status='failed'
                       AND created_at > NOW() - INTERVAL '24h')  AS failed_24h,
                   COUNT(*) AS total
               FROM operation_queue WHERE owner_id=$1""",
            uid,
        )
    except Exception:
        op_stats = None

    # Recent errors from operation_audit
    try:
        errors_24h = await pool.fetchval(
            """SELECT COUNT(*) FROM operation_audit
               WHERE owner_id=$1 AND result != 'success'
               AND occurred_at > NOW() - INTERVAL '24h'""",
            uid,
        ) or 0
    except Exception:
        errors_24h = 0

    # Flood events 24h
    try:
        floods_24h = await pool.fetchval(
            """SELECT COUNT(*) FROM account_flood_log fl
               JOIN tg_accounts a ON a.id=fl.account_id
               WHERE a.owner_id=$1 AND fl.created_at > NOW() - INTERVAL '24h'""",
            uid,
        ) or 0
    except Exception:
        floods_24h = 0

    running = int(op_stats["running"] or 0) if op_stats else 0
    pending = int(op_stats["pending"] or 0) if op_stats else 0
    done_24h = int(op_stats["done_24h"] or 0) if op_stats else 0
    failed_24h = int(op_stats["failed_24h"] or 0) if op_stats else 0
    total_ops = int(op_stats["total"] or 0) if op_stats else 0

    # Pressure score
    try:
        from services import infra_pressure
        pressure = await infra_pressure.compute_pressure(pool, uid)
        p_emoji = pressure.get("level_emoji", "🟢")
        p_score = pressure.get("score", 0)
        p_label = pressure.get("level_label", "Норма")
        pressure_line = f"{p_emoji} Давление: <b>{p_score}/100</b> — {p_label}"
    except Exception:
        pressure_line = ""

    text = (
        "📊 <b>Статистика BotMother OS</b>\n\n"
        f"📱 <b>Аккаунты:</b>\n"
        f"   Активных: <b>{acc_total}</b>"
        + (f"  |  Забанено/спам: <b>{acc_banned}</b>" if acc_banned else "")
        + (f"  |  Кулдаун: <b>{acc_cooldown}</b>" if acc_cooldown else "")
        + "\n\n"
        f"⚙️ <b>Операции:</b>\n"
        f"   🔄 Активных: <b>{running}</b>  ⏳ Ожидают: <b>{pending}</b>\n"
        f"   ✅ Завершено (24ч): <b>{done_24h}</b>  ❌ Ошибок (24ч): <b>{failed_24h}</b>\n"
        f"   Всего в очереди: <b>{total_ops}</b>\n\n"
        f"🛡 <b>Здоровье:</b>\n"
        f"   ⚡ Flood-событий (24ч): <b>{floods_24h}</b>\n"
        f"   📋 Ошибок операций (24ч): <b>{errors_24h}</b>\n"
        + (f"   {pressure_line}\n" if pressure_line else "")
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Аналитика инфраструктуры", callback_data=InfraCb(action="menu"))
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue", op_type="all", page=0))
    kb.button(text="📊 Отчёты по операциям", callback_data=BmCb(action="op_reports"))
    kb.adjust(1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── /find — поиск функции по ключевому слову ─────────────────────────────────

_NAV_MAP: list[tuple[list[str], str, str]] = [
    # keywords, button_text, callback_data (packed via .pack() — точная арность!)
    (["пост", "опубликовать", "publish", "quick post"], "✍️ Быстрый пост", QuickPostCb(action="start").pack()),
    (["рассылка", "broadcast", "сообщение всем"], "📢 Рассылки & Связь", BmCb(action="comms").pack()),
    (["аккаунт", "аккаунты", "account", "телефон"], "📱 Аккаунты", AccCb(action="menu").pack()),
    (["бот", "боты", "мои боты", "bot"], "🤖 Мои боты", BotCb(action="list", page=0).pack()),
    (["канал", "каналы", "channel"], "📡 Каналы", ChanCb(action="menu").pack()),
    (["группа", "группы", "group"], "👥 Группы", GroupFCb(action="menu").pack()),
    (["прокси", "proxy", "vpn"], "🌐 Прокси", ProxyCb(action="menu").pack()),
    (["позиция", "позиции", "поиск", "ranking", "keyword"], "📊 Позиции в поиске", VisCb(action="dashboard").pack()),
    (["продвижение", "promo", "накрутка", "подписчики"], "🚀 Продвижение", PromoCb(action="menu").pack()),
    (["конкурент", "competitors", "analyse"], "🏆 Конкуренты", CompCb(action="menu").pack()),
    (["seo", "сео", "описание", "title"], "📈 SEO", ChanFactCb(action="seo_pick").pack()),
    (["экосистема", "ecosystem", "сеть"], "🌐 Экосистемы", EcoCb(action="menu").pack()),
    (["кластер", "cluster"], "🔗 Кластеры", ClustMCb(action="menu").pack()),
    (["разогрев", "warmup", "прогрев"], "🌡 Разогрев аккаунтов", WarmupCb(action="menu").pack()),
    (["парсер", "аудитория", "parser"], "🔍 Парсер аудитории", ParserCb(action="menu").pack()),
    (["здоровье", "health", "статус аккаунтов"], "❤️ Здоровье", HealthCb(action="menu").pack()),
    (["очередь", "операции", "queue", "задачи"], "📋 Очередь операций", MassOpCb(action="queue").pack()),
    (["масспаблиш", "массовая публикация", "mass publish"], "📤 Массовая публикация", MassPubCb(action="menu").pack()),
    (["шаблон", "template"], "📄 Шаблоны", AssetTplCb(action="menu").pack()),
    (["подписка", "subscription", "тариф", "план", "оплата"], "💳 Подписка", SubCb(action="menu").pack()),
    (["strike", "страйк", "удар"], "⚔️ Strike", StrikeCb(action="menu").pack()),
    (["присутствие", "presence", "global"], "🌍 Присутствие", GeoPresenceCb(action="menu").pack()),
    (["воронка", "funnel", "авторассылка"], "🔗 Воронки", BmCb(action="pick_bot_for", sub="fn").pack()),
    (["авто-ответ", "auto reply", "автоответ"], "💬 Авто-ответы", BmCb(action="pick_bot_for", sub="ar").pack()),
    (["ии", "ai", "искусственный интеллект", "ассистент"], "🤖 ИИ Помощник", AiCb(action="start").pack()),
    (["workspace", "пространство", "команда"], "🏢 Пространства", WorkspaceCb(action="menu").pack()),
    (["реферал", "реф", "referral"], "👥 Рефералы", RefCb(action="menu").pack()),
    (["топология", "topology", "карта"], "🗺️ Топология", TopoCb(action="menu").pack()),
    (["регистрация", "дата", "regdate", "возраст"], "🔍 Дата регистрации", RegCb(action="start").pack()),
    (["анализ", "analyse", "analyze"], "🔬 Полный анализ", RegCb(action="analyze_start").pack()),
    (["dm", "директ", "личное сообщение"], "📨 DM-кампании", DmCb(action="menu").pack()),
]


@router.message(Command("find"))
async def cmd_find(message: Message) -> None:
    """Поиск функции по ключевому слову."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton

    raw = (message.text or "").strip()
    query = raw.removeprefix("/find").strip().lower()

    if not query:
        await message.answer(
            "🔍 <b>Поиск функции</b>\n\n"
            "Введите: <code>/find [ключевое слово]</code>\n\n"
            "Примеры:\n"
            "<code>/find рассылка</code>\n"
            "<code>/find аккаунт</code>\n"
            "<code>/find позиции</code>\n"
            "<code>/find прокси</code>",
            parse_mode="HTML",
        )
        return

    matches: list[tuple[str, str]] = []
    for keywords, label, cb_str in _NAV_MAP:
        if any(kw in query or query in kw for kw in keywords):
            matches.append((label, cb_str))

    if not matches:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не найдено.\n\n"
            "Попробуйте другое слово или откройте меню: /menu",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    for label, cb_str in matches[:8]:
        kb.button(text=label, callback_data=cb_str)
        kb.adjust(1)

    await message.answer(
        f"🔍 <b>Результаты по «{query}»:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

