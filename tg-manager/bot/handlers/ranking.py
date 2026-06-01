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

from bot.callbacks import RankCb, VisCb, BmCb
from bot.keyboards import back_to_bot, subscription_locked_markup
from bot.states import AddKeyword, AddKeywordFSM, KeywordAlertFSM
from bot.utils.op_helpers import safe_edit
from bot.utils.subscription import get_plan, locked_text
from database import db
from services.logger import log_exc_swallow

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
async def cmd_ranking(message: Message) -> None:
    from bot.callbacks import BmCb

    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Открыть BotMother OS", callback_data=BmCb(action="main"))
    await message.answer(
        "📊 <b>Трекер позиций</b>\n\n"
        "Откройте BotMother OS и перейдите в:\n"
        "<code>BotMother → 📊 Аналитика → 📊 Позиции</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
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
        await safe_edit(
            callback,
            locked_text("Трекер позиций в поиске", "starter"),
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
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
        await safe_edit(
            callback,
            locked_text("Трекер позиций в поиске", "starter"),
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
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
    await safe_edit(
        callback,
        "🔑 <b>Добавить ключевое слово</b>\n\n"
        "Введите ключевое слово или фразу для отслеживания:\n\n"
        "<i>Примеры: крипто бот, tg магазин, ai assistant</i>\n"
        "<i>Максимум 50 символов.</i>\n\n"
        "<i>Для отмены нажмите кнопку ниже или /cancel.</i>",
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

    if ok:
        try:
            from services import behavioral_engine

            await behavioral_engine.record_search_repeat(
                pool, message.from_user.id, keyword
            )
        except Exception:
            log_exc_swallow(log, "Не удалось записать поведенческое событие для ключевого слова")

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
    ok = await db.remove_tracked_keyword(
        pool, callback_data.keyword_id, callback.from_user.id
    )
    if not ok:
        await callback.answer(
            "Ключевое слово не найдено или уже удалено.", show_alert=True
        )
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
        await safe_edit(
            callback,
            f"📈 <b>История позиций: «{kw_safe}»</b>\n\n"
            "Данных пока нет. Проверка будет выполнена при следующем цикле.",
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
        prev_pos: int | None = (
            rankings[i + 1]["position"] if i + 1 < len(rankings) else None
        )
        arrow = _trend_arrow(pos, prev_pos)

        bar = _position_bar(pos)
        if pos is None:
            lines.append(f"• {date_label} — <b>не в топ 20</b>{arrow}")
        else:
            lines.append(f"• {date_label} — <b>#{pos}</b>{arrow}  <code>{bar}</code>")

    await safe_edit(
        callback,
        "\n".join(lines),
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
            callback_data=RankCb(
                action="history", bot_id=bot_id, keyword_id=keyword_id
            ),
        )
    else:
        back_kb.button(
            text="◀️ Назад к позициям",
            callback_data=RankCb(action="menu", bot_id=bot_id),
        )

    # Check for active account first
    if not await _has_active_account(pool, owner_id):
        await safe_edit(
            callback,
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для проверки позиций нужен хотя бы один подключённый аккаунт Telegram.\n"
            "Подключите аккаунт через /accounts",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Fetch the bot's username
    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await safe_edit(
            callback,
            "⚠️ Бот не найден.",
            reply_markup=back_kb.as_markup(),
        )
        return

    username = (bot_row.get("username") or "").lstrip("@")
    if not username:
        await safe_edit(
            callback,
            "⚠️ У бота нет username — поиск невозможен.",
            reply_markup=back_kb.as_markup(),
        )
        return

    # Pick least-recently-used active account (fair distribution across accounts)
    account: asyncpg.Record | None = None
    try:
        account = await pool.fetchrow(
            "SELECT id, session_str, device_model, system_version, app_version FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE "
            "ORDER BY last_used ASC NULLS FIRST LIMIT 1",
            owner_id,
        )
    except Exception as exc:
        log.warning("Ошибка при запросе tg_accounts: %s", exc)

    if not account:
        await safe_edit(
            callback,
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для автоматической проверки подключите аккаунт Telegram: /accounts",
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
        await safe_edit(
            callback,
            "ℹ️ Нет ключевых слов для проверки.",
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
                    if (
                        r.get("is_bot")
                        and canonicalize(r.get("username", "")) == entity_id
                    ):
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

                results.append(
                    {
                        "keyword": kw["keyword"],
                        "position": position,
                        "keyword_id": kw["id"],
                    }
                )
            except Exception as exc:
                log.warning(
                    "Ошибка поиска для ключевого слова %r: %s", kw["keyword"], exc
                )
                results.append(
                    {
                        "keyword": kw["keyword"],
                        "position": None,
                        "keyword_id": kw["id"],
                    }
                )

    except ImportError:
        log.warning("account_manager service not available")
        await safe_edit(
            callback,
            "⚠️ Сервис проверки позиций ещё не подключён.\n\n"
            "Позиции будут обновляться автоматически при следующем плановом цикле.",
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
            callback_data=RankCb(
                action="history", bot_id=bot_id, keyword_id=keyword_id
            ),
        )
    result_kb.button(
        text="📊 К позициям",
        callback_data=RankCb(action="menu", bot_id=bot_id),
    )
    result_kb.adjust(1)

    await safe_edit(
        callback,
        "\n".join(lines),
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
        await safe_edit(
            callback,
            "⚠️ <b>Нет подключённого аккаунта</b>\n\n"
            "Для проверки позиций нужен хотя бы один подключённый аккаунт Telegram.\n"
            "Подключите аккаунт через /accounts",
            reply_markup=back_kb.as_markup(),
        )
        return

    try:
        from services import ranking_checker  # type: ignore

        results = await ranking_checker.check_bot_keywords(pool, bot_id, owner_id)
    except ImportError:
        log.warning("ranking_checker service not available")
        await safe_edit(
            callback,
            "⚠️ Сервис проверки позиций недоступен.",
            reply_markup=back_kb.as_markup(),
        )
        return
    except Exception as exc:
        log.warning("check_all error: %s", exc)
        await safe_edit(
            callback,
            "⚠️ Ошибка при проверке. Попробуйте позже.",
            reply_markup=back_kb.as_markup(),
        )
        return

    if not results:
        await safe_edit(
            callback,
            "ℹ️ Нет активных ключевых слов для проверки.",
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

    await safe_edit(
        callback,
        "\n".join(lines),
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
        await safe_edit(
            callback,
            locked_text("Трекер позиций в поиске", "starter"),
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
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
        await safe_edit(
            callback,
            "📊 <b>Дашборд позиций</b>\n\nУ вас пока нет отслеживаемых ключевых слов.",
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
    positions_with_value = [
        e["position"] for e in keywords if e["position"] is not None
    ]
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
        lines.append(
            f"\n<i>...и ещё {total - DASHBOARD_LIMIT} слов (показаны первые {DASHBOARD_LIMIT})</i>"
        )

    await safe_edit(
        callback,
        "\n".join(lines),
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
            callback_data=RankCb(
                action="toggle_keyword", bot_id=bot_id, keyword_id=kw["id"]
            ),
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

    await safe_edit(callback, text, reply_markup=kb.as_markup())


async def _show_rank_menu(
    callback: CallbackQuery,
    bot_id: int,
    pool: asyncpg.Pool,
) -> None:
    """Render the ranking menu; used after removal and other redirects."""
    plan = await get_plan(pool, callback.from_user.id)
    if plan == "free":
        await safe_edit(
            callback,
            locked_text("Трекер позиций в поиске", "starter"),
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
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
    bot_id = callback_data.bot_id
    owner_id = callback.from_user.id

    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]
    notify_on = await db.get_keyword_notify_enabled(pool, bot_id, owner_id)

    status_text = "✅ включены" if notify_on else "❌ выключены"
    toggle_label = (
        "🔕 Выключить уведомления" if notify_on else "🔔 Включить уведомления"
    )

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

    await safe_edit(callback, text, reply_markup=kb.as_markup())


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
        await callback.answer(
            "Нет ключевых слов для изменения настроек.", show_alert=True
        )
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

    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ══════════════════════════════════════════════════════════════════════════════
# VISIBILITY ENGINE
# ══════════════════════════════════════════════════════════════════════════════

# ── VisCb(action="dashboard") — Visibility Dashboard ─────────────────────────


@router.callback_query(VisCb.filter(F.action == "dashboard"))
async def vis_dashboard(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        bot_count_row = await pool.fetchrow(
            """SELECT COUNT(DISTINCT sk.bot_id) AS bots,
                      COUNT(sk.id)              AS keywords,
                      MAX(ph.checked_at)        AS last_check
               FROM search_keywords sk
               LEFT JOIN position_history ph ON ph.bot_id = sk.bot_id
               WHERE sk.owner_id = $1 AND sk.is_active = TRUE""",
            owner_id,
        )
    except Exception as exc:
        log.warning("vis_dashboard DB error: %s", exc)
        bot_count_row = None

    n_bots = bot_count_row["bots"] if bot_count_row else 0
    n_keywords = bot_count_row["keywords"] if bot_count_row else 0
    last_check_raw = bot_count_row["last_check"] if bot_count_row else None

    if last_check_raw:
        if last_check_raw.tzinfo is None:
            last_check_raw = last_check_raw.replace(tzinfo=timezone.utc)
        last_check_str = last_check_raw.strftime("%H:%M")
    else:
        last_check_str = "никогда"

    text = (
        "👁️ <b>Visibility Engine</b>\n\n"
        f"Отслеживается ботов: <b>{n_bots}</b>\n"
        f"Ключевых слов: <b>{n_keywords}</b>\n"
        f"Последняя проверка: <b>{last_check_str}</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Все позиции", callback_data=VisCb(action="all_positions"))
    kb.button(text="🔍 По боту", callback_data=VisCb(action="select_bot"))
    kb.button(text="➕ Добавить слово", callback_data=VisCb(action="add_keyword"))
    kb.button(text="📈 Тренды", callback_data=VisCb(action="trends"))
    kb.button(text="🔔 Настройки алертов", callback_data=VisCb(action="alerts"))
    from bot.callbacks import BmCb as _BmCb

    kb.button(text="◀️ Назад", callback_data=_BmCb(action="analytics"))
    kb.adjust(2, 2, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── VisCb(action="all_positions") — Все позиции всех ботов ───────────────────


@router.callback_query(VisCb.filter(F.action == "all_positions"))
async def vis_all_positions(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT tk.keyword, sr.position, sr.checked_at, b.username AS bot_username
               FROM search_rankings sr
               JOIN tracked_keywords tk ON tk.id = sr.keyword_id
               JOIN managed_bots b ON b.bot_id = sr.bot_id
               WHERE b.added_by = $1
               ORDER BY sr.checked_at DESC
               LIMIT 20""",
            owner_id,
        )
    except Exception as exc:
        log.warning("vis_all_positions DB error: %s", exc)
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=VisCb(action="all_positions"))
    kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
    kb.adjust(2)

    if not rows:
        await safe_edit(
            callback,
            "📊 <b>Позиции в поиске</b>\n\nДанных пока нет. Добавьте ключевые слова и запустите проверку.",
            reply_markup=kb.as_markup(),
        )
        return

    # Group by bot
    from collections import defaultdict

    by_bot: dict[str, list] = defaultdict(list)
    for row in rows:
        by_bot[row["bot_username"] or "unknown"].append(row)

    lines = ["📊 <b>Позиции в поиске</b>\n"]
    for bot_un, entries in by_bot.items():
        bot_safe = html.escape(f"@{bot_un}")
        lines.append(f"\n🤖 {bot_safe}")
        for e in entries:
            kw_safe = html.escape(e["keyword"])
            pos = e["position"]

            # Fetch previous position from position_history for delta
            try:
                prev_row = await pool.fetchrow(
                    """SELECT position FROM position_history
                       WHERE bot_id = (SELECT bot_id FROM managed_bots WHERE username=$1 LIMIT 1)
                         AND keyword = $2
                       ORDER BY checked_at DESC
                       LIMIT 1 OFFSET 1""",
                    bot_un,
                    e["keyword"],
                )
                prev_pos = prev_row["position"] if prev_row else None
            except Exception:
                prev_pos = None

            arrow = _trend_arrow(pos, prev_pos)
            alert = ""
            if prev_pos is not None and pos is not None and pos > prev_pos:
                alert = " ⚠️"
            delta_str = ""
            if prev_pos is not None and pos is not None and pos != prev_pos:
                direction = "↑" if pos < prev_pos else "↓"
                delta_str = f" ({direction} с #{prev_pos})"

            if pos is None:
                lines.append(f"  • «{kw_safe}» → не в топ 20{alert}")
            else:
                lines.append(f"  • «{kw_safe}» → #{pos}{delta_str}{arrow}{alert}")

    await safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )


# ── VisCb(action="select_bot") — Выбрать бота для просмотра позиций ──────────


@router.callback_query(VisCb.filter(F.action == "select_bot"))
async def vis_select_bot(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    bots = await db.get_bots(pool, owner_id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
        await safe_edit(
            callback,
            "👁️ <b>Visibility Engine</b>\n\nУ вас пока нет ботов.",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for bot in bots:
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(
            text=f"🤖 {label}",
            callback_data=VisCb(action="by_bot", bot_id=bot["bot_id"]),
        )
    kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
    kb.adjust(1)

    await safe_edit(
        callback,
        "🔍 <b>Позиции по боту</b>\n\nВыберите бота:",
        reply_markup=kb.as_markup(),
    )


# ── VisCb(action="by_bot", bot_id=X) — Позиции конкретного бота ──────────────


@router.callback_query(VisCb.filter(F.action == "by_bot"))
async def vis_by_bot(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    owner_id = callback.from_user.id
    bot_id = callback_data.bot_id

    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]

    try:
        rows = await pool.fetch(
            """SELECT tk.keyword, sr.position, sr.checked_at
               FROM search_rankings sr
               JOIN tracked_keywords tk ON tk.id = sr.keyword_id
               WHERE sr.bot_id = $1
               ORDER BY sr.checked_at DESC
               LIMIT 30""",
            bot_id,
        )
    except Exception as exc:
        log.warning("vis_by_bot DB error: %s", exc)
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="📈 Тренды", callback_data=VisCb(action="trends", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=VisCb(action="select_bot"))
    kb.adjust(2)

    if not rows:
        await safe_edit(
            callback,
            f"🔍 <b>Позиции — {html.escape(label)}</b>\n\nДанных пока нет.",
            reply_markup=kb.as_markup(),
        )
        return

    # Deduplicate — keep latest entry per keyword
    seen: dict[str, dict] = {}
    for row in rows:
        if row["keyword"] not in seen:
            seen[row["keyword"]] = dict(row)

    lines = [f"🔍 <b>Позиции — {html.escape(label)}</b>\n"]
    for kw_text, entry in seen.items():
        kw_safe = html.escape(kw_text)
        pos = entry["position"]
        try:
            prev_row = await pool.fetchrow(
                """SELECT position FROM position_history
                   WHERE bot_id = $1 AND keyword = $2
                   ORDER BY checked_at DESC
                   LIMIT 1 OFFSET 1""",
                bot_id,
                kw_text,
            )
            prev_pos = prev_row["position"] if prev_row else None
        except Exception:
            prev_pos = None

        arrow = _trend_arrow(pos, prev_pos)
        delta_str = ""
        if prev_pos is not None and pos is not None and pos != prev_pos:
            direction = "↑" if pos < prev_pos else "↓"
            delta_str = f" ({direction} с #{prev_pos})"

        if pos is None:
            lines.append(f"  • «{kw_safe}» → не в топ 20 ❌")
        else:
            lines.append(f"  • «{kw_safe}» → #{pos}{delta_str}{arrow}")

    await safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=kb.as_markup(),
    )


# ── VisCb(action="add_keyword") — Wizard добавления ключевого слова ───────────


@router.callback_query(VisCb.filter(F.action == "add_keyword"))
async def vis_add_keyword_start(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    bots = await db.get_bots(pool, owner_id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
        await safe_edit(
            callback,
            "➕ <b>Добавить ключевое слово</b>\n\nСначала добавьте бота через /start.",
            reply_markup=kb.as_markup(),
        )
        return

    await state.set_state(AddKeywordFSM.choosing_bot)

    kb = InlineKeyboardBuilder()
    for bot in bots:
        label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
        kb.button(
            text=f"🤖 {label}",
            callback_data=VisCb(action="vis_pick_bot", bot_id=bot["bot_id"]),
        )
    kb.button(text="❌ Отмена", callback_data=VisCb(action="dashboard"))
    kb.adjust(1)

    await safe_edit(
        callback,
        "➕ <b>Добавить ключевое слово</b>\n\nШаг 1/3: Выберите бота:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    VisCb.filter(F.action == "vis_pick_bot"), AddKeywordFSM.choosing_bot
)
async def vis_pick_bot(
    callback: CallbackQuery,
    callback_data: VisCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.update_data(vis_bot_id=callback_data.bot_id)
    await state.set_state(AddKeywordFSM.waiting_keyword)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=VisCb(action="dashboard"))

    await safe_edit(
        callback,
        "➕ <b>Добавить ключевое слово</b>\n\n"
        "Шаг 2/3: Введите ключевое слово или фразу:\n\n"
        "<i>Примеры: крипто бот, tg магазин, ai assistant</i>\n"
        "<i>Максимум 50 символов.</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(AddKeywordFSM.waiting_keyword)
async def vis_receive_keyword(
    message: Message,
    state: FSMContext,
) -> None:
    keyword = (message.text or "").strip()
    if not keyword or len(keyword) > 50:
        await message.answer(
            "⚠️ Ключевое слово должно быть от 1 до 50 символов. Попробуйте ещё раз:",
            parse_mode="HTML",
        )
        return

    await state.update_data(vis_keyword=keyword)
    await state.set_state(AddKeywordFSM.waiting_region)

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    region_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇦 UA", callback_data="vis_reg:ua"),
                InlineKeyboardButton(text="🇷🇺 RU", callback_data="vis_reg:ru"),
            ],
            [
                InlineKeyboardButton(text="🇬🇧 EN", callback_data="vis_reg:en"),
                InlineKeyboardButton(text="🌍 Все", callback_data="vis_reg:all"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="vis_reg:cancel")],
        ]
    )

    await message.answer(
        f"➕ <b>Добавить ключевое слово</b>\n\n"
        f"Слово: <b>«{html.escape(keyword)}»</b>\n\n"
        "Шаг 3/3: Выберите регион/язык:",
        parse_mode="HTML",
        reply_markup=region_kb,
    )


@router.callback_query(
    lambda c: c.data and c.data.startswith("vis_reg:"), AddKeywordFSM.waiting_region
)
async def vis_receive_region(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    region_raw = callback.data.split(":", 1)[1]
    await callback.answer()

    if region_raw == "cancel":
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ К дашборду", callback_data=VisCb(action="dashboard"))
        await safe_edit(
            callback,
            "❌ Добавление ключевого слова отменено.",
            reply_markup=kb.as_markup(),
        )
        return

    region = region_raw  # "ua" | "ru" | "en" | "all"
    data = await state.get_data()
    await state.clear()

    bot_id: int = data.get("vis_bot_id", 0)
    keyword: str = data.get("vis_keyword", "")
    owner_id = callback.from_user.id

    if not bot_id or not keyword:
        await safe_edit(
            callback,
            "⚠️ Ошибка: данные не найдены. Начните заново.",
        )
        return

    try:
        await pool.execute(
            """INSERT INTO search_keywords(bot_id, keyword, region, owner_id)
               VALUES($1, $2, $3, $4)
               ON CONFLICT(bot_id, keyword) DO NOTHING""",
            bot_id,
            keyword,
            region,
            owner_id,
        )
        # Also insert into tracked_keywords so ranking_checker picks it up
        await pool.execute(
            "INSERT INTO tracked_keywords(bot_id, owner_id, keyword) VALUES($1,$2,$3) "
            "ON CONFLICT(bot_id, keyword) DO NOTHING",
            bot_id, owner_id, keyword,
        )
        saved = True
    except Exception as exc:
        log.warning("vis_receive_region save error: %s", exc)
        saved = False

    kb = InlineKeyboardBuilder()
    kb.button(text="👁️ Дашборд", callback_data=VisCb(action="dashboard"))
    kb.button(text="➕ Ещё слово", callback_data=VisCb(action="add_keyword"))
    kb.adjust(2)

    kw_safe = html.escape(keyword)
    if saved:
        await safe_edit(
            callback,
            f"✅ Ключевое слово <b>«{kw_safe}»</b> добавлено для региона <b>{region.upper()}</b>.\n\n"
            "Позиция будет определена при следующей проверке.",
            reply_markup=kb.as_markup(),
        )
    else:
        await safe_edit(
            callback,
            f"ℹ️ Слово <b>«{kw_safe}»</b> уже отслеживается для этого бота.",
            reply_markup=kb.as_markup(),
        )


# ── VisCb(action="trends") / VisCb(action="trends", bot_id=X) — Тренды ───────


@router.callback_query(VisCb.filter(F.action == "trends"))
async def vis_trends(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    owner_id = callback.from_user.id
    bot_id = callback_data.bot_id

    if not bot_id:
        await callback.answer()
        bots = await db.get_bots(pool, owner_id)
        if not bots:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
            await safe_edit(
                callback,
                "📈 <b>Тренды позиций</b>\n\nНет ботов.",
                reply_markup=kb.as_markup(),
            )
            return
        if len(bots) == 1:
            bot_id = bots[0]["bot_id"]
        else:
            sel_kb = InlineKeyboardBuilder()
            for bot in bots:
                label = f"@{bot['username']}" if bot["username"] else bot["first_name"]
                sel_kb.button(
                    text=f"🤖 {label}",
                    callback_data=VisCb(action="trends", bot_id=bot["bot_id"]),
                )
            sel_kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
            sel_kb.adjust(1)
            await safe_edit(
                callback,
                "📈 <b>Тренды позиций</b>\n\nВыберите бота:",
                reply_markup=sel_kb.as_markup(),
            )
            return

    bot_row = await db.get_bot(pool, bot_id, owner_id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    label = f"@{bot_row['username']}" if bot_row["username"] else bot_row["first_name"]

    try:
        history_rows = await pool.fetch(
            """SELECT keyword, position, checked_at
               FROM position_history
               WHERE bot_id = $1
               ORDER BY keyword, checked_at DESC""",
            bot_id,
        )
    except Exception as exc:
        log.warning("vis_trends position_history error: %s", exc)
        history_rows = []

    # Fallback to search_rankings if position_history is empty
    if not history_rows:
        try:
            history_rows = await pool.fetch(
                """SELECT tk.keyword, sr.position, sr.checked_at
                   FROM search_rankings sr
                   JOIN tracked_keywords tk ON tk.id = sr.keyword_id
                   WHERE sr.bot_id = $1
                   ORDER BY tk.keyword, sr.checked_at DESC""",
                bot_id,
            )
        except Exception as exc:
            log.warning("vis_trends search_rankings fallback error: %s", exc)
            history_rows = []

    kb2 = InlineKeyboardBuilder()
    kb2.button(text="🔄 Обновить", callback_data=VisCb(action="trends", bot_id=bot_id))
    kb2.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
    kb2.adjust(2)

    if not history_rows:
        await safe_edit(
            callback,
            f"📈 <b>Тренд позиций — {html.escape(label)}</b>\n\nИстория пока пуста.",
            reply_markup=kb2.as_markup(),
        )
        return

    from collections import defaultdict

    kw_history: dict[str, list] = defaultdict(list)
    for row in history_rows:
        kw_history[row["keyword"]].append(row)

    lines = [f"📈 <b>Тренд позиций — {html.escape(label)}</b>\n"]

    for kw_text, entries in list(kw_history.items())[:10]:
        kw_safe = html.escape(kw_text)
        lines.append(f"\n<b>«{kw_safe}»</b>")

        # entries are DESC — reverse for chronological timeline display
        timeline = list(reversed(entries[:7]))
        trend_parts = []
        for entry in timeline:
            pos = entry["position"]
            at = entry["checked_at"]
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            date_label = at.strftime("%d.%m")
            pos_str = f"#{pos}" if pos is not None else "—"
            trend_parts.append(f"{date_label}: {pos_str}")

        trend_line = " → ".join(trend_parts)

        first_pos = timeline[0]["position"] if timeline else None
        last_pos = timeline[-1]["position"] if timeline else None
        if first_pos is not None and last_pos is not None:
            if last_pos < first_pos:
                verdict = " ✅ (улучшение)"
            elif last_pos > first_pos:
                verdict = " ⚠️ (ухудшение)"
            else:
                verdict = " → (без изменений)"
        else:
            verdict = ""

        lines.append(f"{trend_line}{verdict}")

    await safe_edit(
        callback,
        "\n".join(lines),
        reply_markup=kb2.as_markup(),
    )


# ── VisCb(action="alerts") — Настройки алертов ───────────────────────────────


@router.callback_query(VisCb.filter(F.action == "alerts"))
async def vis_alerts(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    await _render_vis_alerts(callback, owner_id, pool)


async def _render_vis_alerts(
    callback: CallbackQuery,
    owner_id: int,
    pool: asyncpg.Pool,
) -> None:
    try:
        row = await pool.fetchrow(
            "SELECT drop_threshold, rise_threshold, alerts_enabled "
            "FROM visibility_alert_settings WHERE owner_id=$1",
            owner_id,
        )
    except Exception as exc:
        log.warning("vis_alerts DB error: %s", exc)
        row = None

    drop_thr = row["drop_threshold"] if row else 10
    rise_thr = row["rise_threshold"] if row else 5
    enabled = row["alerts_enabled"] if row else True

    enabled_icon = "✅ Включено" if enabled else "❌ Выключено"
    toggle_label = "🔕 Выключить алерты" if enabled else "🔔 Включить алерты"

    text = (
        "🔔 <b>Алерты позиций</b>\n\n"
        f"Статус: <b>{enabled_icon}</b>\n\n"
        "Уведомлять если позиция:\n"
        f"• Упала ниже #{drop_thr} → {'✅ Включено' if enabled else '❌ Выключено'}\n"
        f"• Поднялась выше #{rise_thr} → {'✅ Включено' if enabled else '❌ Выключено'}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text=toggle_label, callback_data=VisCb(action="alerts_toggle"))
    kb.button(text="✏️ Изменить порог", callback_data=VisCb(action="alerts_threshold"))
    kb.button(text="◀️ Назад", callback_data=VisCb(action="dashboard"))
    kb.adjust(1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


@router.callback_query(VisCb.filter(F.action == "alerts_toggle"))
async def vis_alerts_toggle(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        row = await pool.fetchrow(
            "SELECT alerts_enabled FROM visibility_alert_settings WHERE owner_id=$1",
            owner_id,
        )
        current = row["alerts_enabled"] if row else True
        new_val = not current
        await pool.execute(
            """INSERT INTO visibility_alert_settings(owner_id, alerts_enabled)
               VALUES($1, $2)
               ON CONFLICT(owner_id) DO UPDATE SET alerts_enabled = EXCLUDED.alerts_enabled""",
            owner_id,
            new_val,
        )
    except Exception as exc:
        log.warning("vis_alerts_toggle DB error: %s", exc)

    await _render_vis_alerts(callback, owner_id, pool)


@router.callback_query(VisCb.filter(F.action == "alerts_threshold"))
async def vis_alerts_threshold(
    callback: CallbackQuery,
    callback_data: VisCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(KeywordAlertFSM.choosing_threshold)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=VisCb(action="alerts"))

    await safe_edit(
        callback,
        "✏️ <b>Изменить порог алертов</b>\n\n"
        "Введите два числа через пробел:\n"
        "<code>порог_падения порог_роста</code>\n\n"
        "<i>Пример: <code>10 5</code>\n"
        "Уведомление при падении ниже #10 и росте выше #5</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(KeywordAlertFSM.choosing_threshold)
async def vis_receive_threshold(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    parts = (message.text or "").strip().split()
    owner_id = message.from_user.id

    try:
        drop_thr = int(parts[0])
        rise_thr = int(parts[1]) if len(parts) > 1 else 5
        assert 1 <= drop_thr <= 50 and 1 <= rise_thr <= 50
    except (ValueError, IndexError, AssertionError):
        await message.answer(
            "⚠️ Неверный формат. Введите два числа от 1 до 50, например: <code>10 5</code>",
            parse_mode="HTML",
        )
        return

    await state.clear()

    try:
        await pool.execute(
            """INSERT INTO visibility_alert_settings(owner_id, drop_threshold, rise_threshold)
               VALUES($1, $2, $3)
               ON CONFLICT(owner_id) DO UPDATE
                 SET drop_threshold = EXCLUDED.drop_threshold,
                     rise_threshold = EXCLUDED.rise_threshold""",
            owner_id,
            drop_thr,
            rise_thr,
        )
        saved = True
    except Exception as exc:
        log.warning("vis_receive_threshold save error: %s", exc)
        saved = False

    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 К настройкам алертов", callback_data=VisCb(action="alerts"))
    kb.button(text="👁️ Дашборд", callback_data=VisCb(action="dashboard"))
    kb.adjust(1)

    if saved:
        await message.answer(
            f"✅ Пороги обновлены:\n"
            f"• Падение: ниже #{drop_thr}\n"
            f"• Рост: выше #{rise_thr}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer(
            "⚠️ Ошибка при сохранении. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
