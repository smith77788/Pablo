"""BotMother — главное Telegram-native OS меню (9 секций)."""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    AccCb,
    AiCb,
    AssetTplCb,
    BmCb,
    BotCb,
    ChanCb,
    ClustMCb,
    CompCb,
    GroupFCb,
    HealthCb,
    MassOpCb,
    NetBcCb,
    NetworkCb,
    ProxyCb,
    RankCb,
    RefCb,
    RelayCb,
    ScheduleCb,
    SubCb,
    AutoReplyCb,
)
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from database import db

log = logging.getLogger(__name__)

router = Router()

# ── Keyboard builders ─────────────────────────────────────────────────────


def _main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏗️ Infrastructure",   callback_data=BmCb(action="infrastructure"))
    kb.button(text="👁️ Visibility",       callback_data=BmCb(action="visibility"))
    kb.button(text="⚙️ Operations",       callback_data=BmCb(action="operations"))
    kb.button(text="📢 Broadcasts",       callback_data=BmCb(action="broadcasts"))
    kb.button(text="💬 Inbox / Relay",    callback_data=BmCb(action="inbox"))
    kb.button(text="🤖 AI Assistant",     callback_data=BmCb(action="ai_assistant"))
    kb.button(text="🧠 Аналитика",        callback_data=BmCb(action="behavioral"))
    kb.button(text="💳 Billing",          callback_data=BmCb(action="billing"))
    kb.button(text="👥 Referral",         callback_data=BmCb(action="referral"))
    kb.button(text="⚙️ Settings",         callback_data=BmCb(action="settings"))
    kb.adjust(2, 2, 2, 2, 2)
    return kb.as_markup()


def _infrastructure_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Аккаунты",          callback_data=AccCb(action="menu"))
    kb.button(text="🤖 Мои боты",          callback_data=BotCb(action="list", page=0))
    kb.button(text="📡 Каналы & операции", callback_data=ChanCb(action="menu"))
    kb.button(text="👥 Группы",            callback_data=GroupFCb(action="menu"))
    kb.button(text="🔗 Кластеры",          callback_data=ClustMCb(action="menu"))
    kb.button(text="🌐 Прокси",            callback_data=ProxyCb(action="menu"))
    kb.button(text="❤️ Здоровье",          callback_data=HealthCb(action="menu"))
    kb.button(text="◀️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def _visibility_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Ключевые слова", callback_data=BmCb(action="pick_bot_for", sub="rank"))
    kb.button(text="📊 Позиции",        callback_data=BmCb(action="pick_bot_for", sub="rank"))
    kb.button(text="🏆 Конкуренты",     callback_data=CompCb(action="menu"))
    kb.button(text="🔔 Алерты",         callback_data=BmCb(action="alerts"))
    kb.button(text="📋 Отчёты",         callback_data=BmCb(action="vis_reports"))
    kb.button(text="◀️ Назад",          callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _operations_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Массовые действия", callback_data=BmCb(action="bulk_ops"))
    kb.button(text="🛠️ Построитель",      callback_data=MassOpCb(action="menu"))
    kb.button(text="📋 Очередь",           callback_data=MassOpCb(action="queue"))
    kb.button(text="⏱️ Планировщик",       callback_data=BmCb(action="op_planner"))
    kb.button(text="📄 Шаблоны",           callback_data=AssetTplCb(action="menu"))
    kb.button(text="📊 Отчёты",            callback_data=BmCb(action="op_reports"))
    kb.button(text="◀️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def _broadcasts_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Рассылка по боту",  callback_data=BotCb(action="list", page=0))
    kb.button(text="🌐 Сетевая рассылка",  callback_data=NetBcCb(action="choose_target"))
    kb.button(text="📅 Расписание",        callback_data=BmCb(action="schedules"))
    kb.button(text="◀️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def _inbox_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Входящие диалоги", callback_data=BmCb(action="pick_bot_for", sub="relay"))
    kb.button(text="◀️ Назад",            callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _settings_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Авто-ответы",   callback_data=BmCb(action="pick_bot_for", sub="ar"))
    kb.button(text="🔔 Уведомления",   callback_data=BmCb(action="notifications"))
    kb.button(text="◀️ Назад",         callback_data=BmCb(action="main"))
    kb.adjust(2, 1)
    return kb.as_markup()


def _bulk_ops_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Боты",         callback_data=NetworkCb(action="menu"))
    kb.button(text="📡 Каналы",       callback_data=ChanCb(action="menu"))
    kb.button(text="📱 Аккаунты",     callback_data=AccCb(action="menu"))
    kb.button(text="◀️ Назад",        callback_data=BmCb(action="operations"))
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def _wip_kb(back_action: str = "main"):
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к меню", callback_data=BmCb(action=back_action))
    kb.adjust(1)
    return kb.as_markup()


_MAIN_MENU_TEXT = (
    "🏠 <b>BotMother OS</b> — Главное меню\n\n"
    "Это операционная система для управления Telegram-активами.\n"
    "Выберите раздел:\n\n"
    "🏗️ <b>Infrastructure</b> — аккаунты, боты, каналы, группы\n"
    "👁️ <b>Visibility</b> — позиции в поиске, конкуренты\n"
    "⚙️ <b>Operations</b> — массовые действия и планировщик\n"
    "📢 <b>Broadcasts</b> — рассылки пользователям ботов\n"
    "💬 <b>Inbox</b> — ответы на входящие сообщения\n"
    "🤖 <b>AI</b> — ИИ-помощник для контента\n"
    "🧠 <b>Аналитика</b> — поведенческий анализ (PRO)\n"
    "💳 <b>Billing</b> — подписка и оплата\n"
    "👥 <b>Referral</b> — пригласить друзей\n"
    "⚙️ <b>Settings</b> — авто-ответы и уведомления"
)

# ── /menu command ─────────────────────────────────────────────────────────


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(
        _MAIN_MENU_TEXT,
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


# ── Helpers ───────────────────────────────────────────────────────────────

async def _edit(callback: CallbackQuery, text: str, markup) -> None:
    """Edit existing message or send new one if message is unavailable."""
    try:
        if callback.message:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.bot.send_message(callback.from_user.id, text,
                                            parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        log.warning("BotMother _edit error: %s", e)
        try:
            await callback.bot.send_message(callback.from_user.id, text,
                                            parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass


# ── Main menu callback ────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "main"))
async def cb_main(callback: CallbackQuery, callback_data: BmCb) -> None:
    log.info("BotMother cb_main from user %s", callback.from_user.id)
    await callback.answer()
    await _edit(callback, _MAIN_MENU_TEXT, _main_menu_kb())


# ── Infrastructure ────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "infrastructure"))
async def cb_infrastructure(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "🏗️ <b>Infrastructure — ваша инфраструктура</b>\n\n"
        "📱 <b>Аккаунты</b> — Telegram-аккаунты для операций\n"
        "🤖 <b>Мои боты</b> — боты с аудиторией, рассылками, воронками\n"
        "📡 <b>Каналы</b> — создание, импорт, публикация в каналы\n"
        "👥 <b>Группы</b> — создание и управление группами\n"
        "🔗 <b>Кластеры</b> — объединить ботов в сеть\n"
        "🌐 <b>Прокси</b> — прокси для аккаунтов\n"
        "❤️ <b>Здоровье</b> — статус аккаунтов и ботов",
        _infrastructure_kb(),
    )


# ── Visibility ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "visibility"))
async def cb_visibility(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "👁️ <b>Visibility — видимость в поиске Telegram</b>\n\n"
        "🔍 <b>Ключевые слова</b> — отслеживать по каким запросам находят ваш бот\n"
        "📊 <b>Позиции</b> — история позиций в поиске Telegram\n"
        "🏆 <b>Конкуренты</b> — анализ конкурирующих ботов\n"
        "🔔 <b>Алерты</b> — уведомления о резких изменениях\n"
        "📋 <b>Отчёты</b> — сводные отчёты за 7/30 дней",
        _visibility_kb(),
    )


# ── Operations ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "operations"))
async def cb_operations(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "⚙️ <b>Operations — массовые операции</b>\n\n"
        "⚡ <b>Массовые действия</b> — join/leave, bulk-edit, инвайт\n"
        "🛠️ <b>Построитель</b> — собрать операцию из блоков\n"
        "📋 <b>Очередь</b> — текущие и завершённые операции\n"
        "⏱️ <b>Планировщик</b> — запустить операцию по расписанию\n"
        "📄 <b>Шаблоны</b> — сохранённые конфигурации операций\n"
        "📊 <b>Отчёты</b> — история и статистика выполненных операций",
        _operations_kb(),
    )


# ── Broadcasts ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "broadcasts"))
async def cb_broadcasts(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "📢 <b>Broadcasts — рассылки</b>\n\n"
        "📢 <b>Рассылка по боту</b> — разослать сообщение всем пользователям бота\n"
        "🌐 <b>Сетевая рассылка</b> — одновременно через несколько ботов\n"
        "📅 <b>Расписание</b> — запланированные рассылки\n\n"
        "<i>Каждый бот имеет свою аудиторию. "
        "Выберите бота → Рассылка → введите текст.</i>",
        _broadcasts_kb(),
    )


# ── Inbox / Relay ─────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "inbox"))
async def cb_inbox(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "💬 <b>Inbox / Relay — входящие сообщения</b>\n\n"
        "Здесь вы можете отвечать на входящие сообщения "
        "пользователей ваших ботов в режиме реального времени.\n\n"
        "<b>Как работает Relay:</b>\n"
        "1. Пользователь пишет вашему боту\n"
        "2. Сообщение приходит вам сюда\n"
        "3. Вы отвечаете — ответ уходит через бота\n\n"
        "Выберите бота для управления входящими:",
        _inbox_kb(),
    )


# ── AI Assistant ──────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "ai_assistant"))
async def cb_ai_assistant(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    # Direct redirect to AI assistant
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Открыть AI-ассистент", callback_data=AiCb(action="start"))
    kb.button(text="◀️ Назад",                callback_data=BmCb(action="main"))
    kb.adjust(1)
    await _edit(callback, "<b>🤖 AI Assistant</b>\n\nИнтеллектуальный помощник для управления ботами.", kb.as_markup())


# ── Billing ───────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "billing"))
async def cb_billing(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Управление подпиской", callback_data=SubCb(action="menu"))
    kb.button(text="◀️ Назад",               callback_data=BmCb(action="main"))
    kb.adjust(1)
    await _edit(callback, "<b>💳 Billing</b>\n\nУправление подпиской и тарифными планами.", kb.as_markup())


# ── Referral ──────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "referral"))
async def cb_referral(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Реферальная программа", callback_data=RefCb(action="menu"))
    kb.button(text="◀️ Назад",                callback_data=BmCb(action="main"))
    kb.adjust(1)
    await _edit(callback, "<b>👥 Referral</b>\n\nРеферальная программа и партнёрские вознаграждения.", kb.as_markup())


# ── Settings ──────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "settings"))
async def cb_settings(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "⚙️ <b>Settings — настройки</b>\n\n"
        "📢 <b>Авто-ответы</b> — автоматически отвечать на ключевые слова\n"
        "🔔 <b>Уведомления</b> — какие события присылать вам\n\n"
        "<i>Авто-ответы настраиваются отдельно для каждого бота.</i>",
        _settings_kb(),
    )


# ── Bulk operations ───────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "bulk_ops"))
async def cb_bulk_ops(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await _edit(
        callback,
        "⚡ <b>Массовые действия</b>\n\n"
        "Массовые операции позволяют управлять множеством объектов одновременно.\n\n"
        "🤖 <b>Боты</b> — массовое редактирование, клонирование настроек\n"
        "📡 <b>Каналы</b> — bulk-join, bulk-leave, приглашение участников\n"
        "📱 <b>Аккаунты</b> — операции через Telegram-аккаунты\n\n"
        "<i>Все операции выполняются с умными задержками для защиты аккаунтов.</i>\n\n"
        "Выберите тип:",
        _bulk_ops_kb(),
    )


# ── Bot picker (Visibility / Inbox / Settings) ───────────────────────────

_PICK_META = {
    "rank":  ("🔍 Трекер позиций",    "visibility"),
    "relay": ("💬 Входящие диалоги",  "inbox"),
    "ar":    ("📢 Авто-ответы",       "settings"),
}


@router.callback_query(BmCb.filter(F.action == "pick_bot_for"))
async def cb_pick_bot_for(
    callback: CallbackQuery,
    callback_data: BmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sub = callback_data.sub or ""
    title, back_action = _PICK_META.get(sub, ("Выберите бота", "main"))

    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action=back_action))
        await _edit(
            callback,
            f"<b>{title}</b>\n\nУ вас нет ботов. Сначала добавьте бота через <b>🤖 Мои боты → ➕ Добавить</b>.",
            kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for bot in bots:
        name = html.escape(bot.get("username") or bot.get("first_name") or f"id{bot['bot_id']}")
        if sub == "rank":
            cd = RankCb(action="menu", bot_id=bot["bot_id"])
        elif sub == "relay":
            cd = RelayCb(action="menu", bot_id=bot["bot_id"])
        else:  # ar
            cd = AutoReplyCb(action="list", bot_id=bot["bot_id"])
        kb.button(text=f"🤖 @{name}", callback_data=cd)
    kb.button(text="◀️ Назад", callback_data=BmCb(action=back_action))
    kb.adjust(1)

    await _edit(callback, f"<b>{title}</b>\n\nВыберите бота:", kb.as_markup())


# ── Alerts (Visibility → Alerts) ─────────────────────────────────────────

@router.callback_query(BmCb.filter(F.action == "alerts"))
async def cb_alerts(
    callback: CallbackQuery,
    callback_data: BmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    page = callback_data.page
    limit = 10
    offset = page * limit
    user_id = callback.from_user.id

    rows = await pool.fetch(
        "SELECT severity, event_type, details, created_at, account_id, bot_id "
        "FROM restriction_events WHERE owner_id=$1 "
        "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        user_id, limit, offset,
    )
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM restriction_events WHERE owner_id=$1", user_id
    ) or 0

    if not rows and page == 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="visibility"))
        await _edit(callback, "<b>🔔 Алерты</b>\n\nАлертов нет. Система работает нормально. ✅", kb.as_markup())
        return

    sev_emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
    lines = []
    for r in rows:
        emoji = sev_emoji.get(r["severity"], "🔔")
        dt = r["created_at"].strftime("%d.%m %H:%M")
        if r.get("account_id"):
            entity = f"acc#{r['account_id']}"
        elif r.get("bot_id"):
            entity = f"bot#{r['bot_id']}"
        else:
            entity = "—"
        etype = html.escape(r["event_type"])
        lines.append(f"{emoji} <code>{dt}</code> {etype} ({entity})")

    total_pages = max(1, -(-total // limit))
    text = f"<b>🔔 Алерты</b>  стр. {page + 1}/{total_pages}\n\n" + "\n".join(lines)

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(kb.button(text="◀️", callback_data=BmCb(action="alerts", page=page - 1)))
    if (page + 1) * limit < total:
        nav.append(kb.button(text="▶️", callback_data=BmCb(action="alerts", page=page + 1)))
    if nav:
        kb.adjust(len(nav))
    kb.button(text="🗑 Очистить всё", callback_data=BmCb(action="alerts_clear"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="visibility"))
    await _edit(callback, text, kb.as_markup())


@router.callback_query(BmCb.filter(F.action == "alerts_clear"))
async def cb_alerts_clear(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM restriction_events WHERE owner_id=$1", callback.from_user.id)
    await callback.answer("Алерты очищены", show_alert=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=BmCb(action="visibility"))
    await _edit(callback, "<b>🔔 Алерты</b>\n\nВсе алерты очищены.", kb.as_markup())


# ── Visibility Reports ────────────────────────────────────────────────────

@router.callback_query(BmCb.filter(F.action == "vis_reports"))
async def cb_vis_reports(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await _edit(callback, locked_text("Отчёты по позициям", "starter"), subscription_locked_markup("starter"))
        return
    await callback.answer()
    kws = await db.get_all_keywords_with_latest_ranking(pool, callback.from_user.id)

    if not kws:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="visibility"))
        await _edit(
            callback,
            "<b>📋 Отчёт по позициям</b>\n\nНет отслеживаемых ключевых слов.\n\n"
            "Добавьте слова через <b>👁️ Visibility → 🔍 Ключевые слова</b>.",
            kb.as_markup(),
        )
        return

    by_bot: dict[str, list] = {}
    for kw in kws:
        bot_u = kw["bot_username"] or f"id{kw['bot_id']}"
        by_bot.setdefault(bot_u, []).append(kw)

    lines: list[str] = []
    for bot_u, items in by_bot.items():
        lines.append(f"\n<b>@{html.escape(bot_u)}</b>")
        for kw in items:
            pos = kw["position"]
            if pos is None:
                pos_str = "—"
            elif pos <= 3:
                pos_str = f"🥇 #{pos}"
            elif pos <= 10:
                pos_str = f"🟢 #{pos}"
            elif pos <= 30:
                pos_str = f"🟡 #{pos}"
            else:
                pos_str = f"🔴 #{pos}"
            kw_text = html.escape(kw["keyword"])
            lines.append(f"  • {kw_text}: {pos_str}")

    text = "<b>📋 Отчёт по позициям в поиске</b>" + "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n\n<i>... (показаны первые результаты)</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=BmCb(action="visibility"))
    await _edit(callback, text, kb.as_markup())


# ── Operation Planner ─────────────────────────────────────────────────────

@router.callback_query(BmCb.filter(F.action == "op_planner"))
async def cb_op_planner(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Массовые операции", callback_data=MassOpCb(action="menu"))
    kb.button(text="📅 Расписание рассылок", callback_data=BmCb(action="schedules"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)
    await _edit(
        callback,
        "<b>⏱️ Планировщик</b>\n\n"
        "Выберите, что хотите запланировать:\n\n"
        "• <b>Массовые операции</b> — редактирование ботов, каналов, аккаунтов\n"
        "• <b>Расписание рассылок</b> — отложенная отправка сообщений подписчикам",
        kb.as_markup(),
    )


# ── Operation Reports ─────────────────────────────────────────────────────

@router.callback_query(BmCb.filter(F.action == "op_reports"))
async def cb_op_reports(
    callback: CallbackQuery,
    callback_data: BmCb,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await _edit(callback, locked_text("Отчёты по операциям", "starter"), subscription_locked_markup("starter"))
        return
    await callback.answer()
    page = callback_data.page
    limit = 8
    offset = page * limit
    user_id = callback.from_user.id

    ops = await pool.fetch(
        "SELECT id, op_type, status, total_items, done_items, created_at, finished_at "
        "FROM operation_queue WHERE owner_id=$1 "
        "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        user_id, limit, offset,
    )
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1", user_id
    ) or 0

    if not ops and page == 0:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
        await _edit(callback, "<b>📊 Отчёты по операциям</b>\n\nОпераций ещё не выполнялось.", kb.as_markup())
        return

    status_emoji = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "cancelled": "🚫"}
    lines = []
    for op in ops:
        emoji = status_emoji.get(op["status"], "❓")
        dt = op["created_at"].strftime("%d.%m %H:%M")
        otype = html.escape(op["op_type"])
        if op["total_items"]:
            progress = f"{op['done_items']}/{op['total_items']}"
        else:
            progress = "—"
        lines.append(f"{emoji} <code>{dt}</code> {otype} [{progress}]")

    total_pages = max(1, -(-total // limit))
    text = f"<b>📊 Отчёты по операциям</b>  стр. {page + 1}/{total_pages}\n\n" + "\n".join(lines)

    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(kb.button(text="◀️", callback_data=BmCb(action="op_reports", page=page - 1)))
    if (page + 1) * limit < total:
        nav.append(kb.button(text="▶️", callback_data=BmCb(action="op_reports", page=page + 1)))
    if nav:
        kb.adjust(len(nav))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    await _edit(callback, text, kb.as_markup())


# ── Schedules (bot picker → ScheduleCb) ──────────────────────────────────

@router.callback_query(BmCb.filter(F.action == "schedules"))
async def cb_schedules(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)

    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="broadcasts"))
        await _edit(
            callback,
            "<b>📅 Расписание рассылок</b>\n\nУ вас нет ботов.\n"
            "Добавьте бота через <b>🏗️ Infrastructure → 🤖 Мои боты</b>.",
            kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for bot in bots:
        name = html.escape(bot.get("username") or bot.get("first_name") or f"id{bot['bot_id']}")
        kb.button(text=f"🤖 @{name}", callback_data=ScheduleCb(action="menu", bot_id=bot["bot_id"]))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="broadcasts"))
    kb.adjust(1)
    await _edit(callback, "<b>📅 Расписание рассылок</b>\n\nВыберите бота:", kb.as_markup())


# ── Notifications ─────────────────────────────────────────────────────────

_NOTIF_SQL: dict[str, str] = {
    "new_user":        "new_user        = NOT new_user",
    "flood_warning":   "flood_warning   = NOT flood_warning",
    "position_change": "position_change = NOT position_change",
    "op_complete":     "op_complete     = NOT op_complete",
    "restriction":     "restriction     = NOT restriction",
}

_NOTIF_LABELS = {
    "new_user":        "Новый пользователь",
    "flood_warning":   "Флуд-предупреждения",
    "position_change": "Изменение позиций",
    "op_complete":     "Завершение операций",
    "restriction":     "Ограничения аккаунтов",
}


async def _get_or_create_notif(pool: asyncpg.Pool, user_id: int) -> asyncpg.Record:
    await pool.execute(
        "INSERT INTO notification_settings(user_id) VALUES($1) ON CONFLICT DO NOTHING",
        user_id,
    )
    return await pool.fetchrow("SELECT * FROM notification_settings WHERE user_id=$1", user_id)


def _notif_kb(row: asyncpg.Record) -> object:
    kb = InlineKeyboardBuilder()
    for field, label in _NOTIF_LABELS.items():
        val = row[field]
        icon = "✅" if val else "❌"
        kb.button(text=f"{icon} {label}", callback_data=BmCb(action="notif_toggle", sub=field))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(1)
    return kb.as_markup()


def _notif_text(row: asyncpg.Record) -> str:
    lines = ["<b>🔔 Настройки уведомлений</b>\n"]
    for field, label in _NOTIF_LABELS.items():
        icon = "✅" if row[field] else "❌"
        lines.append(f"{icon} {label}")
    lines.append("\n<i>Нажмите на пункт, чтобы включить / отключить уведомление.</i>")
    return "\n".join(lines)


@router.callback_query(BmCb.filter(F.action == "notifications"))
async def cb_notifications(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    row = await _get_or_create_notif(pool, callback.from_user.id)
    await _edit(callback, _notif_text(row), _notif_kb(row))


@router.callback_query(BmCb.filter(F.action == "notif_toggle"))
async def cb_notif_toggle(
    callback: CallbackQuery,
    callback_data: BmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    field = callback_data.sub or ""
    toggle_expr = _NOTIF_SQL.get(field)
    if not toggle_expr:
        return

    await pool.execute(
        f"INSERT INTO notification_settings(user_id) VALUES($1) "
        f"ON CONFLICT(user_id) DO UPDATE SET {toggle_expr}, updated_at=now()",
        callback.from_user.id,
    )
    row = await pool.fetchrow("SELECT * FROM notification_settings WHERE user_id=$1", callback.from_user.id)
    await _edit(callback, _notif_text(row), _notif_kb(row))


# ── Behavioral Dashboard ──────────────────────────────────────────────────

_BEHAV_VIEWS = {
    "attention": "📊 Топ по вниманию",
    "habit":     "🔄 Активные привычки",
    "decay":     "📉 Угасающие ресурсы",
    "ecosystem": "🌐 Экосистемные узлы",
    "memory":    "🔍 Поисковая память",
}


def _behavioral_kb(sub: str = "attention") -> object:
    kb = InlineKeyboardBuilder()
    for key, label in _BEHAV_VIEWS.items():
        marker = "▸ " if key == sub else ""
        kb.button(text=f"{marker}{label}", callback_data=BmCb(action="behavioral", sub=key))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(BmCb.filter(F.action == "behavioral"))
async def cb_behavioral(
    callback: CallbackQuery,
    callback_data: BmCb,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await _edit(callback, locked_text("Поведенческая аналитика", "pro"), subscription_locked_markup("pro"))
        return
    await callback.answer()
    sub = callback_data.sub or "attention"
    user_id = callback.from_user.id

    from services import behavioral_engine

    if sub == "memory":
        rows = await behavioral_engine.get_search_memory(pool, user_id)
        if not rows:
            text = "<b>🔍 Поисковая память</b>\n\nДанных ещё нет."
        else:
            lines = ["<b>🔍 Поисковая память</b> — ключевые слова\n"]
            for r in rows:
                score = int(r["affinity_score"])
                bar = "█" * (score // 20) + "░" * (5 - score // 20)
                lines.append(f"• <b>{html.escape(r['keyword'])}</b> [{bar}] ×{r['search_count']}")
            text = "\n".join(lines)
    else:
        score_map = {"attention": "attention_score", "habit": "habit_score",
                     "decay": "decay_rate", "ecosystem": "ecosystem_score"}
        score_field = score_map.get(sub, "attention_score")

        if sub == "decay":
            rows = await pool.fetch(
                "SELECT entity_type, entity_id, decay_rate, updated_at "
                "FROM entity_behavioral_score "
                "WHERE owner_id=$1 AND decay_rate > 0.3 "
                "ORDER BY decay_rate DESC LIMIT 10",
                user_id,
            )
            title = "📉 Угасающие ресурсы"
            label = "decay"
        elif sub == "habit":
            rows = await pool.fetch(
                "SELECT entity_type, entity_id, habit_score, updated_at "
                "FROM entity_behavioral_score "
                "WHERE owner_id=$1 AND habit_score > 60 "
                "ORDER BY habit_score DESC LIMIT 10",
                user_id,
            )
            title = "🔄 Активные привычки"
            label = "habit_score"
        elif sub == "ecosystem":
            rows = await pool.fetch(
                "SELECT entity_type, entity_id, ecosystem_score, updated_at "
                "FROM entity_behavioral_score "
                "WHERE owner_id=$1 AND ecosystem_score > 0 "
                "ORDER BY ecosystem_score DESC LIMIT 10",
                user_id,
            )
            title = "🌐 Экосистемные узлы"
            label = "ecosystem_score"
        else:
            rows = await behavioral_engine.get_top_entities(pool, user_id, score_field)
            title = "📊 Топ по вниманию"
            label = "attention_score"

        if not rows:
            text = f"<b>{title}</b>\n\nДанных ещё нет. Поведенческие оценки обновляются каждые 15 минут."
        else:
            lines = [f"<b>{title}</b>\n"]
            for r in rows:
                etype = r["entity_type"]
                eid = r["entity_id"]
                score_val = r.get(label, 0) or 0
                lines.append(f"• {etype} #{eid} — {score_val:.1f}")
            text = "\n".join(lines)

    await _edit(callback, text, _behavioral_kb(sub))
