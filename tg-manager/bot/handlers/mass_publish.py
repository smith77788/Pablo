"""Mass Publishing wizard.

Sends a post to multiple Telegram channels at once:
  - All channels across all active accounts
  - Filtered by specific account
  - Configurable delay between posts (5s / 30s / 60s)
  - Dry-run mode (count only, no actual send)
  - Publication history from operation_queue table

Entry point: MassPubCb(action="menu")
"""
from __future__ import annotations

import asyncio
import html
import logging
import time

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import MassPubCb
from bot.states import MassPublishFSM2
from bot.utils.op_helpers import (
    _acc_label,
    _get_active_accounts,
    _progress_bar,
    _format_duration,
    _progress_text as _progress_text_base,
)
from services import task_registry as _treg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_STARTER = "starter"

# Timing options: label → delay in seconds
_TIMING_OPTIONS = {
    "delay_5s":   ("⚡ 5 сек (быстро)",     5),
    "delay_30s":  ("🛡️ 30 сек (безопасно)", 30),
    "delay_60s":  ("🐌 60 сек (осторожно)", 60),
    "delay_smart": ("🧠 Умный (30-90 сек)", -1),  # -1 = random
}


# ── Helpers ────────────────────────────────────────────────────────────────


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=MassPubCb(action="menu"))
    return kb


# ── Main menu ──────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Все каналы",    callback_data=MassPubCb(action="start", target_type="all"))
    kb.button(text="👤 По аккаунту",   callback_data=MassPubCb(action="start", target_type="account"))
    kb.button(text="🔍 Сухой прогон",  callback_data=MassPubCb(action="dry_run"))
    kb.button(text="📋 История",       callback_data=MassPubCb(action="history"))
    kb.button(text="◀️ Назад",        callback_data=MassPubCb(action="back_to_factory"))
    kb.adjust(2, 2, 1)
    return kb


@router.callback_query(MassPubCb.filter(F.action == "menu"))
async def cb_mpub_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "📤 <b>Массовая публикация — рассылка в каналы</b>\n\n"
        "Отправляет один пост одновременно во все ваши каналы.\n\n"
        "📢 <b>Все каналы</b> — опубликовать во все каналы всех аккаунтов\n"
        "👤 <b>По аккаунту</b> — выбрать конкретный аккаунт\n"
        "🔍 <b>Сухой прогон</b> — посчитать каналы без реальной отправки\n"
        "📋 <b>История</b> — прошлые публикации\n\n"
        "<i>💡 Сначала импортируйте каналы через "
        "«📡 Каналы → 📥 Импорт из Telegram»</i>",
        parse_mode="HTML",
        reply_markup=_main_menu_kb().as_markup(),
    )


@router.callback_query(MassPubCb.filter(F.action == "back_to_factory"))
async def cb_mpub_back_factory(callback: CallbackQuery) -> None:
    await callback.answer()
    from bot.callbacks import ChanFactCb
    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Channel Factory", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        "◀️ Вернитесь в Channel Factory или воспользуйтесь /ops",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MAIN FLOW: start (all | account)
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassPubCb.filter(F.action == "start"))
async def cb_mpub_start(
    callback: CallbackQuery,
    callback_data: MassPubCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Массовая публикация — STARTER</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    target_type = callback_data.target_type or ""

    # Check for template prefill from asset_templates apply
    sd = await state.get_data()
    prefill = sd.get("tpl_prefill") or {}
    prefill_text = prefill.get("text", "").strip() if isinstance(prefill, dict) else ""

    if target_type == "all":
        # Skip account selection — use all active accounts
        accounts = await _get_active_accounts(pool, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text(
                "⚠️ Нет активных аккаунтов. Подключите через /accounts",
                parse_mode="HTML",
                reply_markup=_back_menu_kb().as_markup(),
            )
            return
        await state.update_data(
            target_type="all",
            target_acc_ids=[a["id"] for a in accounts],
            dry_run=False,
            tpl_prefill=None,
        )
        if prefill_text:
            # Auto-inject text from template, skip text input step
            await state.update_data(post_text=prefill_text)
            await state.set_state(MassPublishFSM2.choosing_timing)
            kb = InlineKeyboardBuilder()
            for key, (label, _) in _TIMING_OPTIONS.items():
                kb.button(text=label, callback_data=MassPubCb(action=f"timing_{key}"))
            kb.button(text="❌ Отмена", callback_data=MassPubCb(action="menu"))
            kb.adjust(2, 2, 1)
            preview = prefill_text[:200] + ("…" if len(prefill_text) > 200 else "")
            await callback.message.edit_text(
                f"📝 <b>Текст из шаблона:</b>\n<i>{preview}</i>\n\n"
                "⏱️ <b>Задержка между постами:</b>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        else:
            await state.set_state(MassPublishFSM2.waiting_text)
            await _ask_post_text(callback, edit=True)

    else:
        # Show account picker
        accounts = await _get_active_accounts(pool, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text(
                "⚠️ Нет активных аккаунтов.",
                parse_mode="HTML",
                reply_markup=_back_menu_kb().as_markup(),
            )
            return
        kb = InlineKeyboardBuilder()
        for acc in accounts:
            kb.button(
                text=_acc_label(acc),
                callback_data=MassPubCb(action="pick_account", target_id=acc["id"]),
            )
        kb.button(text="◀️ Назад", callback_data=MassPubCb(action="menu"))
        kb.adjust(1)
        await state.set_state(MassPublishFSM2.choosing_target)
        await state.update_data(target_type="account", dry_run=False, tpl_prefill=None)
        await callback.message.edit_text(
            "👤 <b>Выберите аккаунт</b>\n\nПубликация будет только в каналы этого аккаунта:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(MassPubCb.filter(F.action == "pick_account"))
async def cb_mpub_pick_account(
    callback: CallbackQuery, callback_data: MassPubCb, state: FSMContext
) -> None:
    await callback.answer()
    sd = await state.get_data()
    prefill = sd.get("tpl_prefill") or {}
    prefill_text = prefill.get("text", "").strip() if isinstance(prefill, dict) else ""

    await state.update_data(target_acc_ids=[callback_data.target_id], tpl_prefill=None)

    if prefill_text:
        await state.update_data(post_text=prefill_text)
        await state.set_state(MassPublishFSM2.choosing_timing)
        kb = InlineKeyboardBuilder()
        for key, (label, _) in _TIMING_OPTIONS.items():
            kb.button(text=label, callback_data=MassPubCb(action=f"timing_{key}"))
        kb.button(text="❌ Отмена", callback_data=MassPubCb(action="menu"))
        kb.adjust(2, 2, 1)
        preview = prefill_text[:200] + ("…" if len(prefill_text) > 200 else "")
        await callback.message.edit_text(
            f"📝 <b>Текст из шаблона:</b>\n<i>{preview}</i>\n\n"
            "⏱️ <b>Задержка между постами:</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await state.set_state(MassPublishFSM2.waiting_text)
        await _ask_post_text(callback, edit=True)


async def _ask_post_text(event, edit: bool = True) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassPubCb(action="menu"))
    text = (
        "📝 <b>Текст поста</b>\n\n"
        "Введите текст публикации.\n"
        "Поддерживается HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>"
    )
    markup = kb.as_markup()
    if hasattr(event, "message"):
        if edit:
            try:
                await event.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
                return
            except Exception:
                log_exc_swallow(log, "сбой edit_text в _ask_post_text")
        await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(MassPublishFSM2.waiting_text)
async def fsm_mpub_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите текст поста:")
        return
    await state.update_data(post_text=text)
    await state.set_state(MassPublishFSM2.choosing_timing)
    kb = InlineKeyboardBuilder()
    for key, (label, _) in _TIMING_OPTIONS.items():
        kb.button(text=label, callback_data=MassPubCb(action=f"timing_{key}"))
    kb.button(text="❌ Отмена", callback_data=MassPubCb(action="menu"))
    kb.adjust(2, 2, 1)
    await message.answer(
        "⏱️ <b>Задержка между постами:</b>\n\n"
        "• <b>5 сек</b> — быстро, риск флуд-бана при большом кол-ве\n"
        "• <b>30 сек</b> — рекомендуется для большинства случаев\n"
        "• <b>60 сек</b> — максимально безопасно\n"
        "• <b>Умный</b> — случайно 30-90 сек, имитирует человека",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassPubCb.filter(F.action.startswith("timing_")))
async def cb_mpub_timing(
    callback: CallbackQuery,
    callback_data: MassPubCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    # Extract timing key from action: "timing_delay_5s" → "delay_5s"
    timing_key = callback_data.action[len("timing_"):]
    delay_s = _TIMING_OPTIONS.get(timing_key, ("", 30))[1]
    await state.update_data(timing_key=timing_key, delay_s=delay_s)
    await state.set_state(MassPublishFSM2.previewing)
    await _show_preview(callback, state, pool)


async def _show_preview(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    acc_ids: list[int] = data.get("target_acc_ids", [])
    delay_s: int = data.get("delay_s", 30)
    post_text: str = data.get("post_text", "")
    dry_run: bool = data.get("dry_run", False)

    # Count total channels
    from services import account_manager
    total_channels = 0
    acc_count = 0
    accounts = await pool.fetch(
        "SELECT id, session_str, first_name, phone FROM tg_accounts "
        "WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        callback.from_user.id, acc_ids,
    )
    for acc in accounts:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
        channels = [d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")]
        total_channels += len(channels)
        if channels:
            acc_count += 1

    effective_delay = 60 if delay_s < 0 else delay_s  # smart = ~60s avg
    estimated_s = total_channels * effective_delay
    timing_label = _TIMING_OPTIONS.get(data.get("timing_key", "delay_30s"), ("30с", 30))[0]

    # Truncate post text for preview
    preview_text = post_text[:300] + ("..." if len(post_text) > 300 else "")

    preview_msg = (
        f"🔍 <b>{'Сухой прогон' if dry_run else 'Предпросмотр публикации'}</b>\n\n"
        f"Целевых каналов: <b>{total_channels}</b> (из {acc_count} аккаунт{'а' if acc_count in (2,3,4) else 'ов' if acc_count != 1 else 'а'})\n"
        f"Задержка: <b>{timing_label}</b>\n"
        f"Расчётное время: ~{_format_duration(estimated_s)}\n\n"
        f"Текст поста:\n"
        f"———\n"
        f"{preview_text}\n"
        f"———"
    )

    kb = InlineKeyboardBuilder()
    if not dry_run:
        kb.button(text="✅ Запустить", callback_data=MassPubCb(action="confirm_send"))
    kb.button(text="❌ Отмена", callback_data=MassPubCb(action="menu"))
    kb.adjust(1)

    await state.set_state(MassPublishFSM2.confirming)
    try:
        await callback.message.edit_text(preview_msg, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(preview_msg, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(MassPubCb.filter(F.action == "confirm_send"))
async def cb_mpub_confirm_send(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю публикацию...")
    data = await state.get_data()
    await state.clear()

    acc_ids: list[int] = data.get("target_acc_ids", [])
    delay_s: int = data.get("delay_s", 30)
    post_text: str = data.get("post_text", "")

    accounts = await pool.fetch(
        "SELECT id, session_str, first_name, phone FROM tg_accounts "
        "WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        callback.from_user.id, acc_ids,
    )

    from services import account_manager
    # Gather all (acc, channel) pairs first
    pairs: list[tuple[asyncpg.Record, dict]] = []
    for acc in accounts:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
        channels = [d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")]
        for ch in channels:
            pairs.append((acc, ch))

    total = len(pairs)
    if total == 0:
        await callback.message.edit_text(
            "ℹ️ Нет каналов для публикации.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    progress_msg = await callback.message.edit_text(
        f"📤 <b>Публикация запущена в фоне</b>\n\n"
        f"Каналов для обработки: <b>{total}</b>\n"
        f"<i>Для отмены: /tasks</i>",
        parse_mode="HTML",
    )

    task = asyncio.create_task(_mpub_bg(
        bot=callback.bot,
        user_id=callback.from_user.id,
        progress_msg=progress_msg,
        pairs=pairs,
        post_text=post_text,
        delay_s=delay_s,
    ))
    _treg.register(callback.from_user.id, "publish", f"Mass publish {total} каналов", task)


async def _mpub_bg(bot, user_id: int, progress_msg, pairs: list, post_text: str, delay_s: int) -> None:
    import random
    from services import account_manager
    ok = 0
    err = 0
    forbidden_count = 0
    total = len(pairs)
    start_ts = time.monotonic()
    try:
        for idx, (acc, ch) in enumerate(pairs, 1):
            ch_id = ch["id"]
            access_hash = ch.get("access_hash", 0) or 0
            result = await account_manager.post_to_channel(
                acc["session_str"], ch_id, post_text, access_hash=access_hash, _acc=acc
            )
            if result.get("banned") or "error" in result:
                err += 1
                if result.get("banned"):
                    forbidden_count += 1
            else:
                ok += 1

            try:
                await progress_msg.edit_text(
                    _progress_text_base("Публикация...", idx, total, ok, err),
                    parse_mode="HTML",
                )
            except Exception:
                log_exc_swallow(log, "сбой progress_msg.edit_text в _mpub_bg")

            if idx < total:
                actual_delay = random.uniform(30, 90) if delay_s < 0 else delay_s
                await asyncio.sleep(actual_delay)

        elapsed = time.monotonic() - start_ts
        hint = ""
        if forbidden_count > 0:
            hint = (
                f"\n\n⚠️ <i>{forbidden_count} канал(ов) — нет прав публикации.</i>"
            )
        await progress_msg.edit_text(
            f"📤 <b>Публикация завершена</b>\n\n"
            f"✅ Успешно: {ok} каналов\n"
            f"❌ Ошибки: {err} канал(ов)"
            f"{hint}\n"
            f"⏱️ Время: {_format_duration(elapsed)}",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
    except asyncio.CancelledError:
        try:
            await bot.send_message(
                user_id,
                f"📤 <b>Публикация отменена</b>\n\n✅ Успешно: {ok}  ❌ Ошибок: {err}",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(log, "сбой send_message при отмене публикации")
    except Exception as exc:
        log.exception("_mpub_bg error user=%s: %s", user_id, exc)
        try:
            await bot.send_message(user_id, f"⚠️ Ошибка публикации: {html.escape(str(exc)[:200])}", parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "сбой send_message при ошибке публикации")


# ══════════════════════════════════════════════════════════════════════════
# DRY RUN
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassPubCb.filter(F.action == "dry_run"))
async def cb_mpub_dry_run(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan
    if not await require_plan(pool, callback.from_user.id, _STARTER):
        await callback.message.edit_text(
            "🔒 <b>Сухой прогон — STARTER</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    await state.update_data(
        target_type="all",
        target_acc_ids=[a["id"] for a in accounts],
        dry_run=True,
        delay_s=30,
        timing_key="delay_30s",
    )
    await state.set_state(MassPublishFSM2.waiting_text)
    await _ask_post_text(callback, edit=True)


# ══════════════════════════════════════════════════════════════════════════
# HISTORY
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassPubCb.filter(F.action == "history"))
async def cb_mpub_history(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    rows: list[asyncpg.Record] = []
    try:
        rows = await pool.fetch(
            "SELECT op_type, status, total_items, done_items, created_at, finished_at "
            "FROM operation_queue "
            "WHERE owner_id=$1 AND op_type='mass_publish' "
            "ORDER BY created_at DESC LIMIT 10",
            callback.from_user.id,
        )
    except Exception:
        rows = []

    if not rows:
        await callback.message.edit_text(
            "📋 <b>История публикаций</b>\n\n"
            "История недоступна (нужна БД) или публикаций ещё не было.\n\n"
            "<i>Записи появятся после первой массовой публикации.</i>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    lines = ["📋 <b>История публикаций (последние 10)</b>\n"]
    for r in rows:
        status_icon = {
            "done": "✅",
            "running": "⏳",
            "failed": "❌",
        }.get(r["status"], "❓")
        done = r["done_items"] or 0
        total = r["total_items"] or 0
        created = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "—"
        lines.append(
            f"{status_icon} {created} — {done}/{total} каналов"
        )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


