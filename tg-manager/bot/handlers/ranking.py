"""Telegram search ranking tracker — tracks bot positions for keywords."""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import RankCb, VisCb
from bot.keyboards import back_to_bot, subscription_locked_markup
from bot.states import AddKeyword, AddKeywordFSM, KeywordAlertFSM
from bot.utils.subscription import get_plan, locked_text
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
        return "не в топ 20"
    return f"позиция #{position}"


def _relative_date(checked_at: datetime) -> str:
    """Return human-readable relative date: сегодня, вчера, N дней назад."""
    now = datetime.now(timezone.utc)
    # Make checked_at timezone-aware if needed
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    delta = (now.date() - checked_at.date()).days
    if delta == 0:
        return f"сегодня в {checked_at.strftime('%H:%M')}"
    if delta == 1:
        return "вчера"
    return f"{delta} дней назад"


async def _has_active_account(pool: asyncpg.Pool, owner_id: int) -> bool:
    """Return True if the user has at least one active TG account."""
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE LIMIT 1",
            owner_id,
        )
        return row is not None
    except Exception as exc:
        log.warning("_has_active_account error: %s", exc)
        return False


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
    kb.button(
        text="📊 Дашборд позиций",
        callback_data=RankCb(action="dashboard", bot_id=0),
    )
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
    plan = await get_plan(pool, callback.from_user.id)
    if plan == "free":
        await callback.answer()
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

    await callback.answer()
    await _render_rank_menu(callback, bot_id, bot_row, plan, pool)


# ── action="add" — ask for keyword ────────────────────────────────────────


@router.callback_query(RankCb.filter(F.action == "add"))
async def cb_rank_add(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    plan = await get_plan(pool, callback.from_user.id)
    limit = KEYWORD_LIMITS.get(plan, 0)
    bot_id = callback_data.bot_id

    if limit == 0:
        await callback.answer()
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

    await callback.answer()
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
        "<i>Максимум 50 символов.</i>\n\n"
        "<i>Для отмены нажмите кнопку ниже или /cancel.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── /cancel — cancel FSM ──────────────────────────────────────────────────


@router.message(Command("cancel"), AddKeyword.waiting_keyword)
async def cmd_cancel_add_keyword(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    bot_id: int = data.get("bot_id", 0)
    await state.clear()
    kb = InlineKeyboardBuilder()
    if bot_id:
        kb.button(
            text="📊 К позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )
    await message.answer(
        "❌ Добавление ключевого слова отменено.",
        parse_mode="HTML",
        reply_markup=kb.as_markup() if bot_id else None,
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
    ok = await db.remove_tracked_keyword(pool, callback_data.keyword_id, callback.from_user.id)
    if not ok:
        await callback.answer("Ключевое слово не найдено или уже удалено.", show_alert=True)
        return

    await callback.answer()
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

    rankings = await db.get_ranking_history(pool, keyword_id, limit=7)

    # Fetch keyword name
    keywords = await db.get_tracked_keywords(pool, bot_id)
    kw_name = next((k["keyword"] for k in keywords if k["id"] == keyword_id), "?")
    kw_safe = html.escape(kw_name)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Проверить сейчас",
        callback_data=RankCb(action="check_now", bot_id=bot_id, keyword_id=keyword_id),
    )
    kb.button(
        text="◀️ Назад к позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    kb.adjust(1)

    if not rankings:
        await callback.message.edit_text(
            f"📈 <b>История позиций: «{kw_safe}»</b>\n\n"
            "Данных пока нет. Проверка будет выполнена при следующем цикле.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines: list[str] = [f"📈 <b>История позиций: «{kw_safe}»</b>\n"]

    # Rankings are ordered DESC (newest first) — compute trend relative to next entry
    for i, entry in enumerate(rankings):
        pos: int | None = entry["position"]
        checked_at: datetime = entry["checked_at"]
        date_label = _relative_date(checked_at)

        # Trend: compare current entry to the previous one (older, i+1)
        prev_pos: int | None = rankings[i + 1]["position"] if i + 1 < len(rankings) else None
        arrow = _trend_arrow(pos, prev_pos)

        bar = _position_bar(pos)
        if pos is None:
            lines.append(f"• {date_label} — <b>не в топ 20</b>{arrow}")
        else:
            lines.append(f"• {date_label} — <b>#{pos}</b>{arrow}  <code>{bar}</code>")

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
    keyword_id = callback_data.keyword_id  # 0 means check all keywords for bot
    owner_id = callback.from_user.id

    back_kb = InlineKeyboardBuilder()
    if keyword_id:
        back_kb.button(
            text="◀️ Назад к истории",
            callback_data=RankCb(action="history", bot_id=bot_id, keyword_id=keyword_id),
        )
    else:
        back_kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )

    # Check for active account first
    if not await _has_active_account(pool, owner_id):
        await callback.message.edit_text(
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для проверки позиций нужен хотя бы один подключённый аккаунт Telegram.\n"
            "Подключите аккаунт через /accounts",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Fetch the bot's username
    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.message.edit_text(
            "⚠️ Бот не найден.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    username = (bot_row.get("username") or "").lstrip("@")
    if not username:
        await callback.message.edit_text(
            "⚠️ У бота нет username — поиск невозможен.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Pick least-recently-used active account (fair distribution across accounts)
    account: asyncpg.Record | None = None
    try:
        account = await pool.fetchrow(
            "SELECT id, session_str FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE "
            "ORDER BY last_used ASC NULLS FIRST LIMIT 1",
            owner_id,
        )
    except Exception as exc:
        log.warning("Ошибка при запросе tg_accounts: %s", exc)

    if not account:
        await callback.message.edit_text(
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для автоматической проверки подключите аккаунт Telegram: /accounts",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Fetch keywords — either one specific or all
    all_keywords = await db.get_tracked_keywords(pool, bot_id)
    if keyword_id:
        keywords = [kw for kw in all_keywords if kw["id"] == keyword_id]
    else:
        keywords = list(all_keywords)

    if not keywords:
        await callback.message.edit_text(
            "ℹ️ Нет ключевых слов для проверки.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Run search — each (keyword × account) is an independent observation unit
    results: list[dict[str, Any]] = []
    try:
        import uuid as _uuid
        from services import account_manager  # type: ignore
        from services.search_observer import canonicalize, process_search_result  # type: ignore

        entity_id = canonicalize(username)
        run_id = str(_uuid.uuid4())

        for kw in keywords:
            try:
                search_results = await account_manager.search_in_telegram(
                    account["session_str"], kw["keyword"], _acc=account
                )

                # Deterministic position lookup for UI display
                position: int | None = None
                for r in search_results:
                    if r.get("is_bot") and canonicalize(r.get("username", "")) == entity_id:
                        position = r["position"]
                        break

                # Write to search_rankings for UI history
                await db.save_ranking(pool, kw["id"], bot_id, position)

                # Feed into the observability pipeline:
                # snapshot → observation → state comparison → change event
                await process_search_result(
                    pool=pool,
                    run_id=run_id,
                    keyword_id=kw["id"],
                    account_id=account["id"],
                    keyword=kw["keyword"],
                    entity_id=entity_id,
                    results=search_results,
                    truncated=len(search_results) >= 20,
                )

                # Mark account as used (least-recently-used rotation)
                await db.update_tg_account_used(pool, account["id"])

                results.append({
                    "keyword": kw["keyword"],
                    "position": position,
                    "keyword_id": kw["id"],
                })
            except Exception as exc:
                log.warning("Ошибка поиска для ключевого слова %r: %s", kw["keyword"], exc)
                results.append({
                    "keyword": kw["keyword"],
                    "position": None,
                    "keyword_id": kw["id"],
                })

    except ImportError:
        log.warning("account_manager service not available")
        await callback.message.edit_text(
            "⚠️ Сервис проверки позиций ещё не подключён.\n\n"
            "Позиции будут обновляться автоматически при следующем плановом цикле.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Show results summary
    username_safe = html.escape(username)
    lines = [f"🔄 <b>Результаты проверки — @{username_safe}</b>\n"]
    for r in results:
        kw_safe = html.escape(r["keyword"])
        pos = r["position"]
        if pos is None:
            lines.append(f"🔑 <b>{kw_safe}</b> — не в топ 20 ❌")
        else:
            lines.append(f"🔑 <b>{kw_safe}</b> — позиция #{pos} ✅")

    result_kb = InlineKeyboardBuilder()
    if keyword_id and len(results) == 1:
        result_kb.button(
            text="📈 История позиций",
            callback_data=RankCb(action="history", bot_id=bot_id, keyword_id=keyword_id),
        )
    result_kb.button(
        text="📊 К позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    result_kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=result_kb.as_markup(),
    )


# ── action="check_all" — check all keywords for a bot immediately ──────────


@router.callback_query(RankCb.filter(F.action == "check_all"))
async def cb_rank_check_all(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("⏳ Проверяю все ключевые слова...")

    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    back_kb = InlineKeyboardBuilder()
    back_kb.button(
        text="◀️ Назад к позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )

    # Warn if no active accounts
    if not await _has_active_account(pool, owner_id):
        await callback.message.edit_text(
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для проверки позиций нужен хотя бы один подключённый аккаунт Telegram.\n"
            "Подключите аккаунт через /accounts",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    try:
        from services import ranking_checker  # type: ignore

        results = await ranking_checker.check_bot_keywords(pool, bot_id, owner_id)
    except ImportError:
        log.warning("ranking_checker service not available")
        await callback.message.edit_text(
            "⚠️ Сервис проверки позиций недоступен.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return
    except Exception as exc:
        log.warning("check_all error: %s", exc)
        await callback.message.edit_text(
            "⚠️ Ошибка при проверке. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    if not results:
        await callback.message.edit_text(
            "ℹ️ Нет активных ключевых слов для проверки.",
            parse_mode="HTML",
            reply_markup=back_kb.as_markup(),
        )
        return

    n = len(results)
    lines: list[str] = [f"✅ <b>Проверено {n} ключевых слов</b>\n"]
    for r in results:
        kw_safe = html.escape(r["keyword"])
        pos = r["position"]
        if r.get("error"):
            lines.append(f"🔑 <b>{kw_safe}</b> — ошибка проверки ⚠️")
        elif pos is None:
            lines.append(f"🔑 <b>{kw_safe}</b> — не в топ 20 ❌")
        else:
            lines.append(f"🔑 <b>{kw_safe}</b> — позиция #{pos} ✅")

    result_kb = InlineKeyboardBuilder()
    result_kb.button(
        text="📊 К позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    result_kb.button(
        text="🔄 Проверить снова",
        callback_data=RankCb(action="check_all", bot_id=bot_id),
    )
    result_kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=result_kb.as_markup(),
    )


# ── action="dashboard" — общая таблица позиций по всем ботам ─────────────


DASHBOARD_LIMIT = 20


@router.callback_query(RankCb.filter(F.action == "dashboard"))
async def cb_rank_dashboard(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    owner_id = callback.from_user.id
    plan = await get_plan(pool, owner_id)
    if plan == "free":
        await callback.message.edit_text(
            locked_text("Трекер позиций в поиске", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return

    keywords = await db.get_all_keywords_with_latest_ranking(pool, owner_id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Обновить дашборд",
        callback_data=RankCb(action="dashboard", bot_id=0),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=RankCb(action="menu", bot_id=0),
    )
    kb.adjust(1)

    if not keywords:
        await callback.message.edit_text(
            "📊 <b>Дашборд позиций</b>\n\nУ вас пока нет отслеживаемых ключевых слов.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # Sort: entries with a position first (ascending), then without (❓)
    with_pos = sorted(
        [e for e in keywords if e["position"] is not None],
        key=lambda e: e["position"],
    )
    without_pos = [e for e in keywords if e["position"] is None]
    sorted_keywords = with_pos + without_pos

    total = len(sorted_keywords)
    display = sorted_keywords[:DASHBOARD_LIMIT]

    # Compute average rating for entries that have a position
    positions_with_value = [e["position"] for e in keywords if e["position"] is not None]
    avg_line = ""
    if positions_with_value:
        avg = sum(positions_with_value) / len(positions_with_value)
        avg_line = f"Средняя позиция: <b>#{avg:.1f}</b> по {len(positions_with_value)} из {total} слов\n\n"

    lines: list[str] = [f"📊 <b>Дашборд позиций</b>\n\n{avg_line}"]

    for entry in display:
        bot_un = entry["bot_username"] or "?"
        bot_safe = html.escape(f"@{bot_un}")
        kw_safe = html.escape(entry["keyword"])
        pos = entry["position"]
        if pos is None:
            lines.append(f"❓ {bot_safe} — «{kw_safe}»")
        else:
            lines.append(f"#{pos} {bot_safe} — «{kw_safe}»")

    if total > DASHBOARD_LIMIT:
        lines.append(f"\n<i>...и ещё {total - DASHBOARD_LIMIT} слов (показаны первые {DASHBOARD_LIMIT})</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── action="toggle_keyword" — пауза/возобновление ключевого слова ─────────


@router.callback_query(RankCb.filter(F.action == "toggle_keyword"))
async def cb_rank_toggle_keyword(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    keyword_id = callback_data.keyword_id
    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    new_state = await db.toggle_keyword_active(pool, keyword_id, owner_id)
    if new_state is None:
        await callback.answer("Ключевое слово не найдено.", show_alert=True)
        return

    await callback.answer()
    await _show_rank_menu(callback, bot_id, pool)


# ── Internal helper (shared between menu and post-remove) ─────────────────


async def _render_rank_menu(
    callback: CallbackQuery,
    bot_id: int,
    bot_row: asyncpg.Record,
    plan: str,
    pool: asyncpg.Pool,
) -> None:
    """Build and render the ranking menu message."""
    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    keywords = await db.get_tracked_keywords(pool, bot_id)
    limit = KEYWORD_LIMITS.get(plan, 0)
    owner_id = callback.from_user.id

    # Build keyword lines
    kw_lines: list[str] = []
    for kw in keywords:
        latest = await db.get_latest_ranking(pool, kw["id"])
        history = await db.get_keyword_rankings(pool, kw["id"], limit=2)

        cur_pos = latest["position"] if latest else None
        # history[0] == latest entry, history[1] == previous entry
        prev_pos = history[1]["position"] if len(history) >= 2 else None
        arrow = _trend_arrow(cur_pos, prev_pos)

        kw_safe = html.escape(kw["keyword"])
        status_icon = "✅" if kw["is_active"] else "⏸"

        if cur_pos is None:
            pos_text = f"не в топ 20{arrow} ❌"
        else:
            trend_detail = ""
            if prev_pos is not None and prev_pos != cur_pos:
                trend_detail = f" (с #{prev_pos})"
            pos_text = f"#{cur_pos}{trend_detail}{arrow} ✅"

        kw_lines.append(f"🔑 <b>{kw_safe}</b> — {pos_text} {status_icon}")

    kw_block = "\n".join(kw_lines) if kw_lines else "Нет отслеживаемых ключевых слов."
    limit_display = "∞" if limit >= 9999 else str(limit)
    plan_upper = plan.upper()

    # Account warning
    no_account_warning = ""
    if not await _has_active_account(pool, owner_id):
        no_account_warning = (
            "\n\n⚠️ <b>Нет подключённого аккаунта.</b> "
            "Для проверки позиций нужен хотя бы один подключённый аккаунт → /accounts"
        )

    text = (
        f"📊 <b>Позиции в поиске — {html.escape(label)}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Трекер показывает на какой позиции ваш бот появляется в поиске Telegram по ключевым словам. "
        "Позиции обновляются автоматически если подключён аккаунт Telegram.\n\n"
        f"Ключевых слов: <b>{len(keywords)} из {limit_display}</b> ({plan_upper})\n\n"
        f"{kw_block}"
        f"{no_account_warning}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Добавить слово",
        callback_data=RankCb(action="add", bot_id=bot_id),
    )
    kb.button(
        text="🔄 Проверить все сейчас",
        callback_data=RankCb(action="check_all", bot_id=bot_id),
    )
    kb.button(
        text="🔔 Уведомления",
        callback_data=RankCb(action="notify_settings", bot_id=bot_id),
    )
    for kw in keywords:
        kw_safe_btn = kw["keyword"][:20]
        pause_label = "▶️ Возобновить" if not kw["is_active"] else "⏸ Пауза"
        kb.button(
            text=f"📈 {kw_safe_btn}",
            callback_data=RankCb(action="history", bot_id=bot_id, keyword_id=kw["id"]),
        )
        kb.button(
            text=pause_label,
            callback_data=RankCb(action="toggle_keyword", bot_id=bot_id, keyword_id=kw["id"]),
        )
        kb.button(
            text=f"🗑 {kw_safe_btn}",
            callback_data=RankCb(action="remove", bot_id=bot_id, keyword_id=kw["id"]),
        )
    # Layout: [add, check_all], [notify], then [3 buttons per keyword]
    if keywords:
        kb.adjust(2, 1, *([3] * len(keywords)))
    else:
        kb.adjust(2, 1)

    kb.row(*back_to_bot(bot_id).inline_keyboard[0])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


async def _show_rank_menu(
    callback: CallbackQuery,
    bot_id: int,
    pool: asyncpg.Pool,
) -> None:
    """Render the ranking menu; used after removal and other redirects."""
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

    await _render_rank_menu(callback, bot_id, bot_row, plan, pool)


# ── action="notify_settings" — управление уведомлениями о позиции ─────────


@router.callback_query(RankCb.filter(F.action == "notify_settings"))
async def cb_rank_notify_settings(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    notify_on = await db.get_keyword_notify_enabled(pool, bot_id, owner_id)

    status_text = "✅ включены" if notify_on else "❌ выключены"
    toggle_label = "🔕 Выключить уведомления" if notify_on else "🔔 Включить уведомления"

    text = (
        f"🔔 <b>Уведомления об изменении позиции — {html.escape(label)}</b>\n\n"
        f"Статус: <b>{status_text}</b>\n\n"
        "Бот отправит уведомление, если:\n"
        "• позиция улучшилась на 3+ пункта 📈\n"
        "• позиция ухудшилась на 5+ пунктов 📉\n"
        "• бот появился в топ-20 🎉\n"
        "• бот выпал из топ-20 ⚠️"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text=toggle_label,
        callback_data=RankCb(action="notify_toggle", bot_id=bot_id),
    )
    kb.button(
        text="◀️ Назад к позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── action="notify_toggle" — переключить уведомления ──────────────────────


@router.callback_query(RankCb.filter(F.action == "notify_toggle"))
async def cb_rank_notify_toggle(
    callback: CallbackQuery,
    callback_data: RankCb,
    pool: asyncpg.Pool,
) -> None:
    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    new_val = await db.toggle_keyword_notify(pool, bot_id, owner_id)
    if new_val is None:
        await callback.answer("Нет ключевых слов для изменения настроек.", show_alert=True)
        return

    status = "включены ✅" if new_val else "выключены ❌"
    await callback.answer(f"Уведомления {status}", show_alert=False)

    # Re-render the notify_settings screen
    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        return

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    toggle_label = "🔕 Выключить уведомления" if new_val else "🔔 Включить уведомления"
    status_text = "✅ включены" if new_val else "❌ выключены"

    text = (
        f"🔔 <b>Уведомления об изменении позиции — {html.escape(label)}</b>\n\n"
        f"Статус: <b>{status_text}</b>\n\n"
        "Бот отправит уведомление, если:\n"
        "• позиция улучшилась на 3+ пункта 📈\n"
        "• позиция ухудшилась на 5+ пунктов 📉\n"
        "• бот появился в топ-20 🎉\n"
        "• бот выпал из топ-20 ⚠️"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text=toggle_label,
        callback_data=RankCb(action="notify_toggle", bot_id=bot_id),
    )
    kb.button(
        text="◀️ Назад к позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
