"""BotMother — главное Telegram-native OS меню (9 секций)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    AccCb,
    AiCb,
    BmCb,
    BotCb,
    ChanCb,
    ClustMCb,
    CompCb,
    HealthCb,
    NetBcCb,
    NetworkCb,
    ProxyCb,
    RankCb,
    RefCb,
    RelayCb,
    SubCb,
    AutoReplyCb,
)

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
    kb.button(text="💳 Billing",          callback_data=BmCb(action="billing"))
    kb.button(text="👥 Referral",         callback_data=BmCb(action="referral"))
    kb.button(text="⚙️ Settings",         callback_data=BmCb(action="settings"))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def _infrastructure_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Аккаунты",          callback_data=AccCb(action="menu"))
    kb.button(text="🤖 Мои боты",          callback_data=BotCb(action="list", page=0))
    kb.button(text="📡 Каналы & операции", callback_data=ChanCb(action="menu"))
    kb.button(text="👥 Группы",            callback_data=BmCb(action="groups"))
    kb.button(text="🔗 Кластеры",          callback_data=ClustMCb(action="menu"))
    kb.button(text="🌐 Прокси",            callback_data=ProxyCb(action="menu"))
    kb.button(text="❤️ Здоровье",          callback_data=HealthCb(action="menu"))
    kb.button(text="◀️ Назад",             callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1, 1)
    return kb.as_markup()


def _visibility_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Ключевые слова", callback_data=RankCb(action="menu", bot_id=0))
    kb.button(text="📊 Позиции",        callback_data=RankCb(action="list", bot_id=0))
    kb.button(text="🏆 Конкуренты",     callback_data=CompCb(action="menu"))
    kb.button(text="🔔 Алерты",         callback_data=BmCb(action="alerts"))
    kb.button(text="📋 Отчёты",         callback_data=BmCb(action="vis_reports"))
    kb.button(text="◀️ Назад",          callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _operations_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Массовые действия", callback_data=BmCb(action="bulk_ops"))
    kb.button(text="🛠️ Построитель",      callback_data=BmCb(action="op_builder"))
    kb.button(text="📋 Очередь",           callback_data=BmCb(action="op_queue"))
    kb.button(text="⏱️ Планировщик",       callback_data=BmCb(action="op_planner"))
    kb.button(text="📄 Шаблоны",           callback_data=BmCb(action="op_templates"))
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
    kb.button(text="💬 Входящие диалоги", callback_data=RelayCb(action="menu", bot_id=0))
    kb.button(text="◀️ Назад",            callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _settings_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Авто-ответы",   callback_data=AutoReplyCb(action="list", bot_id=0))
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


# ── /menu command ─────────────────────────────────────────────────────────


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer(
        "<b>🏠 BotMother OS</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


# ── Main menu callback ────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "main"))
async def cb_main(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>🏠 BotMother OS</b>\n\nВыберите раздел:",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


# ── Infrastructure ────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "infrastructure"))
async def cb_infrastructure(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>🏗️ Infrastructure</b>\n\nУправляйте аккаунтами, ботами, каналами и сетевой инфраструктурой.",
        parse_mode="HTML",
        reply_markup=_infrastructure_kb(),
    )


# ── Visibility ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "visibility"))
async def cb_visibility(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>👁️ Visibility</b>\n\nОтслеживайте позиции ботов в поиске Telegram и анализируйте конкурентов.",
        parse_mode="HTML",
        reply_markup=_visibility_kb(),
    )


# ── Operations ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "operations"))
async def cb_operations(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>⚙️ Operations</b>\n\nМассовые действия, построитель операций и планировщик задач.",
        parse_mode="HTML",
        reply_markup=_operations_kb(),
    )


# ── Broadcasts ────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "broadcasts"))
async def cb_broadcasts(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>📢 Broadcasts</b>\n\nРассылки по боту, сетевые рассылки и расписания.",
        parse_mode="HTML",
        reply_markup=_broadcasts_kb(),
    )


# ── Inbox / Relay ─────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "inbox"))
async def cb_inbox(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>💬 Inbox / Relay</b>\n\nВходящие диалоги и реле-переписка с пользователями.",
        parse_mode="HTML",
        reply_markup=_inbox_kb(),
    )


# ── AI Assistant ──────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "ai_assistant"))
async def cb_ai_assistant(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    # Direct redirect to AI assistant
    from aiogram.types import InlineKeyboardMarkup
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Открыть AI-ассистент", callback_data=AiCb(action="start"))
    kb.button(text="◀️ Назад",                callback_data=BmCb(action="main"))
    kb.adjust(1)
    await callback.message.edit_text(
        "<b>🤖 AI Assistant</b>\n\nИнтеллектуальный помощник для управления ботами.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Billing ───────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "billing"))
async def cb_billing(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Управление подпиской", callback_data=SubCb(action="menu"))
    kb.button(text="◀️ Назад",               callback_data=BmCb(action="main"))
    kb.adjust(1)
    await callback.message.edit_text(
        "<b>💳 Billing</b>\n\nУправление подпиской и тарифными планами.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Referral ──────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "referral"))
async def cb_referral(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Реферальная программа", callback_data=RefCb(action="menu"))
    kb.button(text="◀️ Назад",                callback_data=BmCb(action="main"))
    kb.adjust(1)
    await callback.message.edit_text(
        "<b>👥 Referral</b>\n\nРеферальная программа и партнёрские вознаграждения.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Settings ──────────────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "settings"))
async def cb_settings(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>⚙️ Settings</b>\n\nНастройки авто-ответов, уведомлений и системных параметров.",
        parse_mode="HTML",
        reply_markup=_settings_kb(),
    )


# ── Bulk operations ───────────────────────────────────────────────────────


@router.callback_query(BmCb.filter(F.action == "bulk_ops"))
async def cb_bulk_ops(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "<b>⚡ Массовые действия</b>\n\nВыберите тип объекта:",
        parse_mode="HTML",
        reply_markup=_bulk_ops_kb(),
    )


# ── WIP stubs ─────────────────────────────────────────────────────────────

_WIP_ACTIONS = {
    "groups":        ("👥 Группы",         "operations"),
    "alerts":        ("🔔 Алерты",         "visibility"),
    "vis_reports":   ("📋 Отчёты",         "visibility"),
    "op_builder":    ("🛠️ Построитель",    "operations"),
    "op_queue":      ("📋 Очередь",        "operations"),
    "op_planner":    ("⏱️ Планировщик",    "operations"),
    "op_templates":  ("📄 Шаблоны",        "operations"),
    "op_reports":    ("📊 Отчёты",         "operations"),
    "schedules":     ("📅 Расписание",     "broadcasts"),
    "notifications": ("🔔 Уведомления",   "settings"),
}


@router.callback_query(BmCb.filter(F.action.in_(_WIP_ACTIONS)))
async def cb_wip(callback: CallbackQuery, callback_data: BmCb) -> None:
    await callback.answer()
    action = callback_data.action
    title, back_action = _WIP_ACTIONS.get(action, (action, "main"))
    await callback.message.edit_text(
        f"<b>{title}</b>\n\n"
        "🚧 <b>В разработке</b>\n\n"
        "Эта функция будет доступна в следующем обновлении.",
        parse_mode="HTML",
        reply_markup=_wip_kb(back_action),
    )
