"""Semantic Memory CRM hub — operator UI for per-user per-bot conversational memory."""

from __future__ import annotations

import logging
from html import escape as he

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

from bot.callbacks import BmCb, MemCb
from database import db
from services import semantic_memory

router = Router()
log = logging.getLogger(__name__)


# ── FSM states ───────────────────────────────────────────────────────────────


class MemSearch(StatesGroup):
    waiting_user_id = State()


class MemSetDays(StatesGroup):
    waiting_days = State()


# ── Keyboards ────────────────────────────────────────────────────────────────


def _hub_menu(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Поиск по user_id", callback_data=MemCb(action="search", bot_id=bot_id))
    kb.button(text="📊 Статистика памяти", callback_data=MemCb(action="stats", bot_id=bot_id))
    kb.button(text="⚙️ Настройки памяти", callback_data=MemCb(action="settings", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(1)
    return kb.as_markup()


def _back_to_hub(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К памяти", callback_data=MemCb(action="menu", bot_id=bot_id))
    return kb.as_markup()


def _user_profile_kb(bot_id: int, user_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Очистить память пользователя",
        callback_data=MemCb(action="clear_confirm", bot_id=bot_id, user_id=user_id),
    )
    kb.button(
        text="🔍 Другой пользователь",
        callback_data=MemCb(action="search", bot_id=bot_id),
    )
    kb.button(text="◀️ К памяти", callback_data=MemCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


def _clear_confirm_kb(bot_id: int, user_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=MemCb(action="clear_do", bot_id=bot_id, user_id=user_id),
    )
    kb.button(
        text="❌ Отмена",
        callback_data=MemCb(action="view_user", bot_id=bot_id, user_id=user_id),
    )
    kb.adjust(2)
    return kb.as_markup()


def _settings_kb(bot_id: int, enabled: bool, auto_extract: bool) -> object:
    kb = InlineKeyboardBuilder()
    toggle_label = "🔴 Выключить память" if enabled else "🟢 Включить память"
    extract_label = "🔴 Выключить авто-факты" if auto_extract else "🟢 Включить авто-факты"
    kb.button(text=toggle_label, callback_data=MemCb(action="toggle_enabled", bot_id=bot_id))
    kb.button(text="📅 Изменить дни хранения", callback_data=MemCb(action="set_days", bot_id=bot_id))
    kb.button(text=extract_label, callback_data=MemCb(action="toggle_extract", bot_id=bot_id))
    kb.button(text="◀️ К памяти", callback_data=MemCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


# ── Helper: resolve bot for current operator ─────────────────────────────────


async def _resolve_bot(pool: asyncpg.Pool, bot_id: int, owner_id: int) -> dict | None:
    """Return managed bot row if it belongs to owner_id, else None."""
    return await db.get_bot(pool, bot_id, owner_id)


# ── Menu handler ─────────────────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "menu"))
async def cb_mem_menu(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id

    # If no bot_id supplied, show list of user's bots to pick from
    if not bot_id:
        bots = await db.get_bots(pool, callback.from_user.id)
        kb = InlineKeyboardBuilder()
        for b in bots[:20]:
            label = b.get("username") or b.get("first_name") or str(b["bot_id"])
            kb.button(
                text=f"🤖 @{label}" if b.get("username") else f"🤖 {label}",
                callback_data=MemCb(action="menu", bot_id=b["bot_id"]),
            )
        kb.button(text="◀️ Аналитика", callback_data=BmCb(action="analytics"))
        kb.adjust(1)
        text = (
            "🧠 <b>Semantic Memory CRM</b>\n\n"
            "Память позволяет боту помнить каждого пользователя — "
            "его имя, интересы, прошлые вопросы — и отвечать с полным контекстом.\n\n"
            + ("Выберите бота:" if bots else "У вас нет ботов. Добавьте бота через /start.")
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        return

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text(
            "❌ Бот не найден.", parse_mode="HTML", reply_markup=_back_to_hub(0)
        )
        return

    label = row.get("username") or row.get("first_name") or str(bot_id)
    safe_label = he(f"@{label}" if row.get("username") else label)

    stats = await semantic_memory.get_stats(pool, bot_id)
    settings = await semantic_memory.get_settings(pool, bot_id)
    status = "✅ Включена" if settings.get("enabled", True) else "🔴 Выключена"

    text = (
        f"🧠 <b>Semantic Memory — {safe_label}</b>\n\n"
        f"Статус: {status}\n"
        f"👥 Пользователей с памятью: <b>{stats['total_users']}</b>\n"
        f"💬 Сообщений в базе: <b>{stats['total_messages']}</b>\n"
        f"📝 Среднее на пользователя: <b>{stats['avg_messages_per_user']}</b>\n"
        f"🏷 Извлечённых фактов: <b>{stats['total_facts']}</b>\n\n"
        "Память позволяет боту помнить каждого пользователя — его имя, интересы, "
        "прошлые покупки и вопросы — и отвечать с полным контекстом."
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_hub_menu(bot_id),
    )


# ── Search handler ────────────────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "search"))
async def cb_mem_search(
    callback: CallbackQuery,
    callback_data: MemCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(MemSearch.waiting_user_id)
    await state.update_data(bot_id=callback_data.bot_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MemCb(action="menu", bot_id=callback_data.bot_id))

    await callback.message.edit_text(
        "🔍 <b>Поиск пользователя в памяти</b>\n\n"
        "Отправьте <b>user_id</b> (числовой Telegram ID) пользователя:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MemSearch.waiting_user_id, F.text)
async def msg_mem_search_id(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    data = await state.get_data()
    await state.clear()
    bot_id = data.get("bot_id", 0)

    raw = message.text.strip()
    if not raw.lstrip("-").isdigit():
        await message.answer(
            "❌ Неверный формат. Введите числовой Telegram ID пользователя.",
            reply_markup=_back_to_hub(bot_id),
        )
        return

    user_id = int(raw)
    await _show_user_profile(message, pool, bot_id, user_id)


async def _show_user_profile(
    message: Message,
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
) -> None:
    history = await semantic_memory.get_user_history(pool, bot_id, user_id, limit=20)
    facts = await semantic_memory.get_user_facts(pool, bot_id, user_id)

    if not history and not facts:
        await message.answer(
            f"📭 У пользователя <code>{user_id}</code> нет записей в памяти для этого бота.",
            parse_mode="HTML",
            reply_markup=_back_to_hub(bot_id),
        )
        return

    lines = [f"👤 <b>Профиль пользователя</b> <code>{user_id}</code>"]

    if facts:
        lines.append("\n🏷 <b>Факты:</b>")
        for f in facts:
            label = semantic_memory._FACT_LABELS.get(f["fact_key"], f["fact_key"])
            lines.append(f"  • {he(label)}: {he(str(f['fact_value']))}")

    if history:
        lines.append(f"\n💬 <b>Последние {len(history)} сообщений:</b>")
        for h in history[-10:]:
            role_icon = "👤" if h["role"] == "user" else "🤖"
            ts = h["created_at"].strftime("%d.%m %H:%M") if h.get("created_at") else ""
            text_snippet = he(str(h["text"])[:200])
            lines.append(f"  {role_icon} <i>{ts}</i>\n    {text_snippet}")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_user_profile_kb(bot_id, user_id),
    )


# ── View user (from callback) ─────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "view_user"))
async def cb_mem_view_user(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id
    user_id = callback_data.user_id

    history = await semantic_memory.get_user_history(pool, bot_id, user_id, limit=20)
    facts = await semantic_memory.get_user_facts(pool, bot_id, user_id)

    if not history and not facts:
        await callback.message.edit_text(
            f"📭 У пользователя <code>{user_id}</code> нет записей в памяти.",
            parse_mode="HTML",
            reply_markup=_back_to_hub(bot_id),
        )
        return

    lines = [f"👤 <b>Профиль пользователя</b> <code>{user_id}</code>"]

    if facts:
        lines.append("\n🏷 <b>Факты:</b>")
        for f in facts:
            label = semantic_memory._FACT_LABELS.get(f["fact_key"], f["fact_key"])
            lines.append(f"  • {he(label)}: {he(str(f['fact_value']))}")

    if history:
        lines.append(f"\n💬 <b>Последние {len(history)} сообщений:</b>")
        for h in history[-10:]:
            role_icon = "👤" if h["role"] == "user" else "🤖"
            ts = h["created_at"].strftime("%d.%m %H:%M") if h.get("created_at") else ""
            text_snippet = he(str(h["text"])[:200])
            lines.append(f"  {role_icon} <i>{ts}</i>\n    {text_snippet}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_user_profile_kb(bot_id, user_id),
    )


# ── Statistics screen ─────────────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "stats"))
async def cb_mem_stats(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Бот не найден.", reply_markup=_back_to_hub(0))
        return

    stats = await semantic_memory.get_stats(pool, bot_id)
    settings = await semantic_memory.get_settings(pool, bot_id)

    label = row.get("username") or row.get("first_name") or str(bot_id)
    safe_label = he(f"@{label}" if row.get("username") else label)

    text = (
        f"📊 <b>Статистика памяти — {safe_label}</b>\n\n"
        f"👥 Уникальных пользователей: <b>{stats['total_users']}</b>\n"
        f"💬 Всего сообщений: <b>{stats['total_messages']}</b>\n"
        f"📈 Среднее на пользователя: <b>{stats['avg_messages_per_user']}</b>\n"
        f"🏷 Извлечённых фактов: <b>{stats['total_facts']}</b>\n\n"
        f"⚙️ Хранить записи: <b>{settings.get('max_history_days', 90)} дней</b>\n"
        f"🧠 Авто-извлечение фактов: "
        f"<b>{'Вкл' if settings.get('auto_extract_facts', True) else 'Выкл'}</b>"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_back_to_hub(bot_id),
    )


# ── Settings screen ───────────────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "settings"))
async def cb_mem_settings(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.message.edit_text("❌ Бот не найден.", reply_markup=_back_to_hub(0))
        return

    settings = await semantic_memory.get_settings(pool, bot_id)
    enabled = settings.get("enabled", True)
    auto_extract = settings.get("auto_extract_facts", True)
    max_days = settings.get("max_history_days", 90)

    label = row.get("username") or row.get("first_name") or str(bot_id)
    safe_label = he(f"@{label}" if row.get("username") else label)

    text = (
        f"⚙️ <b>Настройки памяти — {safe_label}</b>\n\n"
        f"Статус: <b>{'✅ Включена' if enabled else '🔴 Выключена'}</b>\n"
        f"Хранить записи: <b>{max_days} дней</b>\n"
        f"Авто-извлечение фактов: <b>{'✅ Вкл' if auto_extract else '🔴 Выкл'}</b>\n\n"
        "<i>Авто-извлечение фактов — AI анализирует диалоги и сохраняет ключевую информацию "
        "об имени, интересах, покупках пользователя.</i>"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_settings_kb(bot_id, enabled, auto_extract),
    )


@router.callback_query(MemCb.filter(F.action == "toggle_enabled"))
async def cb_mem_toggle_enabled(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    bot_id = callback_data.bot_id

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.answer("❌ Бот не найден.", show_alert=True)
        return
    await callback.answer()

    settings = await semantic_memory.get_settings(pool, bot_id)
    new_enabled = not settings.get("enabled", True)
    await semantic_memory.upsert_settings(pool, bot_id, enabled=new_enabled)

    status = "✅ Память включена" if new_enabled else "🔴 Память выключена"
    await callback.answer(status, show_alert=True)

    # Refresh settings screen
    callback_data_refresh = MemCb(action="settings", bot_id=bot_id)
    settings_new = await semantic_memory.get_settings(pool, bot_id)
    enabled_n = settings_new.get("enabled", True)
    auto_extract_n = settings_new.get("auto_extract_facts", True)
    max_days_n = settings_new.get("max_history_days", 90)
    label = row.get("username") or row.get("first_name") or str(bot_id)
    safe_label = he(f"@{label}" if row.get("username") else label)
    text = (
        f"⚙️ <b>Настройки памяти — {safe_label}</b>\n\n"
        f"Статус: <b>{'✅ Включена' if enabled_n else '🔴 Выключена'}</b>\n"
        f"Хранить записи: <b>{max_days_n} дней</b>\n"
        f"Авто-извлечение фактов: <b>{'✅ Вкл' if auto_extract_n else '🔴 Выкл'}</b>\n\n"
        "<i>Авто-извлечение фактов — AI анализирует диалоги и сохраняет ключевую информацию "
        "об имени, интересах, покупках пользователя.</i>"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_settings_kb(bot_id, enabled_n, auto_extract_n),
    )


@router.callback_query(MemCb.filter(F.action == "toggle_extract"))
async def cb_mem_toggle_extract(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    bot_id = callback_data.bot_id

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.answer("❌ Бот не найден.", show_alert=True)
        return
    await callback.answer()

    settings = await semantic_memory.get_settings(pool, bot_id)
    new_extract = not settings.get("auto_extract_facts", True)
    await semantic_memory.upsert_settings(pool, bot_id, auto_extract_facts=new_extract)

    label = row.get("username") or row.get("first_name") or str(bot_id)
    safe_label = he(f"@{label}" if row.get("username") else label)
    settings_new = await semantic_memory.get_settings(pool, bot_id)
    enabled_n = settings_new.get("enabled", True)
    auto_extract_n = settings_new.get("auto_extract_facts", True)
    max_days_n = settings_new.get("max_history_days", 90)
    text = (
        f"⚙️ <b>Настройки памяти — {safe_label}</b>\n\n"
        f"Статус: <b>{'✅ Включена' if enabled_n else '🔴 Выключена'}</b>\n"
        f"Хранить записи: <b>{max_days_n} дней</b>\n"
        f"Авто-извлечение фактов: <b>{'✅ Вкл' if auto_extract_n else '🔴 Выкл'}</b>\n\n"
        "<i>Авто-извлечение фактов — AI анализирует диалоги и сохраняет ключевую информацию "
        "об имени, интересах, покупках пользователя.</i>"
    )
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_settings_kb(bot_id, enabled_n, auto_extract_n),
    )


@router.callback_query(MemCb.filter(F.action == "set_days"))
async def cb_mem_set_days(
    callback: CallbackQuery,
    callback_data: MemCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(MemSetDays.waiting_days)
    await state.update_data(bot_id=callback_data.bot_id)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MemCb(action="settings", bot_id=callback_data.bot_id))
    await callback.message.edit_text(
        "📅 <b>Срок хранения записей</b>\n\n"
        "Введите количество дней (от 7 до 365).\n"
        "По умолчанию: <b>90 дней</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MemSetDays.waiting_days, F.text)
async def msg_mem_set_days(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    data = await state.get_data()
    await state.clear()
    bot_id = data.get("bot_id", 0)

    raw = message.text.strip()
    if not raw.isdigit() or not (7 <= int(raw) <= 365):
        await message.answer(
            "❌ Введите число от 7 до 365.",
            reply_markup=_back_to_hub(bot_id),
        )
        return

    days = int(raw)
    await semantic_memory.upsert_settings(pool, bot_id, max_history_days=days)
    await message.answer(
        f"✅ Срок хранения обновлён: <b>{days} дней</b>.",
        parse_mode="HTML",
        reply_markup=_back_to_hub(bot_id),
    )


# ── Clear memory ──────────────────────────────────────────────────────────────


@router.callback_query(MemCb.filter(F.action == "clear_confirm"))
async def cb_mem_clear_confirm(
    callback: CallbackQuery,
    callback_data: MemCb,
) -> None:
    await callback.answer()
    user_id = callback_data.user_id
    bot_id = callback_data.bot_id
    await callback.message.edit_text(
        f"⚠️ <b>Удалить память пользователя <code>{user_id}</code>?</b>\n\n"
        "Будут удалены все сообщения и факты. Действие необратимо.",
        parse_mode="HTML",
        reply_markup=_clear_confirm_kb(bot_id, user_id),
    )


@router.callback_query(MemCb.filter(F.action == "clear_do"))
async def cb_mem_clear_do(
    callback: CallbackQuery,
    callback_data: MemCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.bot_id
    user_id = callback_data.user_id

    row = await _resolve_bot(pool, bot_id, callback.from_user.id)
    if not row:
        await callback.answer("❌ Бот не найден.", show_alert=True)
        return

    deleted_mem, deleted_facts = await semantic_memory.clear_user_memory(
        pool, bot_id, user_id
    )
    await callback.message.edit_text(
        f"🗑 <b>Память пользователя <code>{user_id}</code> очищена</b>\n\n"
        f"Удалено сообщений: <b>{deleted_mem}</b>\n"
        f"Удалено фактов: <b>{deleted_facts}</b>",
        parse_mode="HTML",
        reply_markup=_back_to_hub(bot_id),
    )
    log.info(
        "semantic_memory: operator %d cleared memory for user=%d bot=%d "
        "(%d messages, %d facts)",
        callback.from_user.id,
        user_id,
        bot_id,
        deleted_mem,
        deleted_facts,
    )
