"""Telegram search ranking tracker — tracks bot positions for keywords."""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, RankCb
from bot.keyboards import back_to_bot, subscription_locked_markup
from bot.states import AddKeyword
from bot.utils.subscription import get_plan, locked_text, require_plan
from database import db

log = logging.getLogger(__name__)

router = Router()

# ── Subscription limits ────────────────────────────────────────────────────

KEYWORD_LIMITS: dict[str, int] = {
    "free": 0,
    "starter": 5,
    "pro": 20,
    "enterprise": 9999,
}


# ── Helpers ───────────────────────────────────────────────────────────────

MAX_POSITION = 20
BAR_WIDTH = 12


def _position_bar(position: int | None) -> str:
    """Return an emoji bar for position 1–20; None → «н/д»."""
    if position is None:
        return "н/д"
    # position 1 = full bar, position 20 = 1 block
    filled = max(1, round((MAX_POSITION - position + 1) / MAX_POSITION * BAR_WIDTH))
    empty = BAR_WIDTH - filled
    return "█" * filled + "░" * empty


def _trend_arrow(latest: int | None, previous: int | None) -> str:
    """↑ improved (lower number), ↓ dropped, → same, '' if no comparison."""
    if latest is None or previous is None:
        return ""
    if latest < previous:
        return " ↑"
    if latest > previous:
        return " ↓"
    return " →"


def _format_position(position: int | None) -> str:
    if position is None:
        return "не найден в топ-20"
    return f"позиция #{position}"


# ── /ranking command — pick a bot ─────────────────────────────────────────


@router.message(Command("ranking"))
async def cmd_ranking(message: Message, pool: asyncpg.Pool) -> None:
    plan = await get_plan(pool, message.from_user.id)
    if plan == "free":
        await message.answer(
            locked_text("Трекер позиций в поиске", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    bots = await db.get_bots(pool, message.from_user.id)
    if not bots:
        await message.answer(
            "📊 <b>Трекер позиций</b>\n\nУ вас пока нет ботов. Добавьте первого через /start.",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    for bot in bots:
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(
            text=f"🤖 {label}",
            callback_data=RankCb(action="menu", bot_id=bot["bot_id"]),
        )
    kb.adjust(1)
    await message.answer(
        "📊 <b>Трекер позиций в поиске</b>\n\nВыберите бота:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── action="menu" — keyword list with current positions ───────────────────


@router.callback_query(RankCb.filter(F.action == "menu"))
async def cb_rank_menu(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    plan = await get_plan(pool, callback.from_user.id)
    if plan == "free":
        await callback.message.edit_text(
            locked_text("Трекер позиций в поиске", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    bot_id = callback_data.bot_id
    bot_row = await db.get_bot(pool, bot_id, callback.from_user.id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    keywords = await db.get_tracked_keywords(pool, bot_id)
    limit = KEYWORD_LIMITS.get(plan, 0)

    # Build keyword lines
    kw_lines: list[str] = []
    for kw in keywords:
        latest = await db.get_latest_ranking(pool, kw["id"])
        history = await db.get_keyword_rankings(pool, kw["id"], limit=2)

        cur_pos = latest["position"] if latest else None
        prev_pos = history[1]["position"] if len(history) >= 2 else None
        arrow = _trend_arrow(cur_pos, prev_pos)

        kw_safe = html.escape(kw["keyword"])
        status_icon = "✅" if kw["is_active"] else "⏸"

        if cur_pos is None:
            pos_text = "не найден в топ-20 ❌"
        else:
            trend_detail = ""
            if prev_pos and prev_pos != cur_pos:
                trend_detail = f" (с #{prev_pos}{arrow})"
            pos_text = f"позиция #{cur_pos}{trend_detail} ✅"

        kw_lines.append(f"🔑 <b>{kw_safe}</b> — {pos_text} {status_icon}")

    kw_block = "\n".join(kw_lines) if kw_lines else "Нет отслеживаемых ключевых слов."
    limit_display = "∞" if limit >= 9999 else str(limit)
    plan_upper = plan.upper()

    text = (
        f"📊 <b>Позиции в поиске — {html.escape(label)}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Трекер показывает на какой позиции ваш бот появляется в поиске Telegram по ключевым словам. "
        "Позиции обновляются автоматически если подключён аккаунт Telegram.\n\n"
        f"Ключевых слов: <b>{len(keywords)} из {limit_display}</b> ({plan_upper})\n\n"
        f"{kw_block}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить слово",
        callback_data=RankCb(action="add", bot_id=bot_id),
    )
    kb.button(
        text="🔄 Проверить сейчас",
        callback_data=RankCb(action="check_now", bot_id=bot_id),
    )
    kb.adjust(2)
    for kw in keywords:
        kw_safe_btn = kw["keyword"][:20]
        kb.button(
            text=f"📈 {kw_safe_btn}",
            callback_data=RankCb(action="history", bot_id=bot_id, keyword_id=kw["id"]),
        )
        kb.button(
            text=f"🗑 {kw_safe_btn}",
            callback_data=RankCb(action="remove", bot_id=bot_id, keyword_id=kw["id"]),
        )
    if keywords:
        kb.adjust(2, *([2] * len(keywords)))

    kb.row(*back_to_bot(bot_id).inline_keyboard[0])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── action="add" — ask for keyword ────────────────────────────────────────


@router.callback_query(RankCb.filter(F.action == "add"))
async def cb_rank_add(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()

    plan = await get_plan(pool, callback.from_user.id)
    limit = KEYWORD_LIMITS.get(plan, 0)
    bot_id = callback_data.bot_id

    if limit == 0:
        await callback.message.edit_text(
            locked_text("Трекер позиций в поиске", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    keywords = await db.get_tracked_keywords(pool, bot_id)
    if len(keywords) >= limit:
        limit_display = "∞" if limit >= 9999 else str(limit)
        await callback.answer(
            f"Достигнут лимит: {len(keywords)} из {limit_display} слов для плана {plan.upper()}. "
            "Перейдите на более высокий тариф.",
            show_alert=True,
        )
        return

    await state.set_state(AddKeyword.waiting_keyword)
    await state.update_data(bot_id=bot_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    await callback.message.edit_text(
        "🔑 <b>Добавить ключевое слово</b>\n\n"
        "Введите ключевое слово или фразу для отслеживания:\n\n"
        "<i>Примеры: крипто бот, tg магазин, ai assistant</i>\n"
        "<i>Максимум 50 символов.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── FSM: receive keyword text ──────────────────────────────────────────────


@router.message(AddKeyword.waiting_keyword)
async def msg_add_keyword(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    data = await state.get_data()
    bot_id: int = data.get("bot_id", 0)
    await state.clear()

    keyword = (message.text or "").strip()
    if not keyword:
        await message.answer(
            "⚠️ Пустое сообщение. Попробуйте ещё раз через меню позиций.",
            parse_mode="HTML",
        )
        return

    if len(keyword) > 50:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="📊 К позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
        await message.answer(
            f"⚠️ Слишком длинная фраза ({len(keyword)} симв.). Максимум — 50 символов.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    ok = await db.add_tracked_keyword(pool, bot_id, message.from_user.id, keyword)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 К позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )

    kw_safe = html.escape(keyword)
    if ok:
        await message.answer(
            f"✅ Ключевое слово <b>«{kw_safe}»</b> добавлено.\n\n"
            "Позиция будет определена при следующей автоматической проверке.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer(
            f"ℹ️ Слово <b>«{kw_safe}»</b> уже отслеживается.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── action="remove" — delete keyword ──────────────────────────────────────


@router.callback_query(RankCb.filter(F.action == "remove"))
async def cb_rank_remove(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    ok = await db.remove_tracked_keyword(pool, callback_data.keyword_id, callback.from_user.id)
    if not ok:
        await callback.answer("Ключевое слово не найдено или уже удалено.", show_alert=True)

    await _show_rank_menu(callback, callback_data.bot_id, pool)


# ── action="history" — position history chart ─────────────────────────────


@router.callback_query(RankCb.filter(F.action == "history"))
async def cb_rank_history(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    keyword_id = callback_data.keyword_id
    bot_id = callback_data.bot_id

    rankings = await db.get_keyword_rankings(pool, keyword_id, limit=10)

    # Fetch keyword name
    keywords = await db.get_tracked_keywords(pool, bot_id)
    kw_name = next((k["keyword"] for k in keywords if k["id"] == keyword_id), "?")
    kw_safe = html.escape(kw_name)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад к позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )

    if not rankings:
        await callback.message.edit_text(
            f"📈 <b>История позиций: «{kw_safe}»</b>\n\n"
            "Данных пока нет. Проверка будет выполнена при следующем цикле.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines: list[str] = [f"📈 <b>История позиций: «{kw_safe}»</b>\n"]
    for entry in rankings:
        pos: int | None = entry["position"]
        checked_at: datetime = entry["checked_at"]
        date_str = checked_at.strftime("%d.%m")
        bar = _position_bar(pos)
        if pos is None:
            lines.append(f"<code>{date_str} — н/д</code>")
        else:
            lines.append(f"<code>{date_str} — #{pos:<2}  {bar}</code>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── action="check_now" — trigger immediate search check ───────────────────


@router.callback_query(RankCb.filter(F.action == "check_now"))
async def cb_rank_check_now(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("⏳ Запускаю проверку...")

    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    # Verify TG credentials are configured
    tg_api_id = os.environ.get("TG_API_ID", "")
    tg_api_hash = os.environ.get("TG_API_HASH", "")
    if not tg_api_id or not tg_api_hash:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
        await callback.message.edit_text(
            "⚠️ <b>Настройки не заполнены</b>\n\n"
            "Для проверки позиций необходимы переменные окружения "
            "<code>TG_API_ID</code> и <code>TG_API_HASH</code>.\n\n"
            "Обратитесь к администратору.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Fetch the bot's username
    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    username = bot_row.get("username") or ""
    if not username:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
        await callback.message.edit_text(
            "⚠️ У бота нет username — поиск невозможен.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Find a connected TG account for this user
    account: asyncpg.Record | None = None
    try:
        account = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE LIMIT 1",
            owner_id,
        )
    except Exception as exc:
        log.warning("Could not query tg_accounts: %s", exc)

    if not account:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
        await callback.message.edit_text(
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для автоматической проверки подключите аккаунт Telegram: /accounts",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Fetch keywords
    keywords = await db.get_tracked_keywords(pool, bot_id)
    if not keywords:
        await callback.answer("Нет ключевых слов для проверки.", show_alert=True)
        return

    # Run search via account_manager
    results: list[dict[str, Any]] = []
    try:
        from services import account_manager  # type: ignore

        for kw in keywords:
            try:
                position = await account_manager.search_bots(
                    account, kw["keyword"], target_username=username
                )
                await db.save_ranking(pool, kw["id"], position)
                results.append({
                    "keyword": kw["keyword"],
                    "position": position,
                })
            except Exception as exc:
                log.warning("search_bots failed for %r: %s", kw["keyword"], exc)
                results.append({"keyword": kw["keyword"], "position": None})

    except ImportError:
        # account_manager not yet implemented — gracefully degrade
        log.warning("account_manager service not available")
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
        await callback.message.edit_text(
            "⚠️ Сервис проверки позиций ещё не подключён.\n\n"
            "Позиции будут обновляться автоматически при следующем плановом цикле.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Show results summary
    lines = [f"🔄 <b>Результаты проверки — @{html.escape(username)}</b>\n"]
    for r in results:
        kw_safe = html.escape(r["keyword"])
        pos = r["position"]
        if pos is None:
            lines.append(f"🔑 <b>{kw_safe}</b> — не найден в топ-20 ❌")
        else:
            lines.append(f"🔑 <b>{kw_safe}</b> — позиция #{pos} ✅")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 К позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Internal helper (shared between menu and post-remove) ─────────────────


async def _show_rank_menu(
    callback: CallbackQuery,
    bot_id: int,
    pool: asyncpg.Pool,
) -> None:
    """Render the ranking menu; extracted so remove can reuse it."""
    plan = await get_plan(pool, callback.from_user.id)
    if plan == "free":
        await callback.message.edit_text(
            locked_text("Трекер позиций в поиске", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    bot_row = await db.get_bot(pool, bot_id, callback.from_user.id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    keywords = await db.get_tracked_keywords(pool, bot_id)
    limit = KEYWORD_LIMITS.get(plan, 0)

    kw_lines: list[str] = []
    for kw in keywords:
        latest = await db.get_latest_ranking(pool, kw["id"])
        history = await db.get_keyword_rankings(pool, kw["id"], limit=2)

        cur_pos = latest["position"] if latest else None
        prev_pos = history[1]["position"] if len(history) >= 2 else None
        arrow = _trend_arrow(cur_pos, prev_pos)

        kw_safe = html.escape(kw["keyword"])
        status_icon = "✅" if kw["is_active"] else "⏸"

        if cur_pos is None:
            pos_text = "не найден в топ-20 ❌"
        else:
            trend_detail = ""
            if prev_pos and prev_pos != cur_pos:
                trend_detail = f" (с #{prev_pos}{arrow})"
            pos_text = f"позиция #{cur_pos}{trend_detail} ✅"

        kw_lines.append(f"🔑 <b>{kw_safe}</b> — {pos_text} {status_icon}")

    kw_block = "\n".join(kw_lines) if kw_lines else "Нет отслеживаемых ключевых слов."
    limit_display = "∞" if limit >= 9999 else str(limit)
    plan_upper = plan.upper()

    text = (
        f"📊 <b>Позиции в поиске — {html.escape(label)}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Трекер показывает на какой позиции ваш бот появляется в поиске Telegram по ключевым словам. "
        "Позиции обновляются автоматически если подключён аккаунт Telegram.\n\n"
        f"Ключевых слов: <b>{len(keywords)} из {limit_display}</b> ({plan_upper})\n\n"
        f"{kw_block}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить слово",
        callback_data=RankCb(action="add", bot_id=bot_id),
    )
    kb.button(
        text="🔄 Проверить сейчас",
        callback_data=RankCb(action="check_now", bot_id=bot_id),
    )
    kb.adjust(2)
    for kw in keywords:
        kw_short = kw["keyword"][:20]
        kb.button(
            text=f"📈 {kw_short}",
            callback_data=RankCb(action="history", bot_id=bot_id, keyword_id=kw["id"]),
        )
        kb.button(
            text=f"🗑 {kw_short}",
            callback_data=RankCb(action="remove", bot_id=bot_id, keyword_id=kw["id"]),
        )
    if keywords:
        kb.adjust(2, *([2] * len(keywords)))

    kb.row(*back_to_bot(bot_id).inline_keyboard[0])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
