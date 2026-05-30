"""Mass Operations handler.

Provides:
  - Mass publish: post to multiple channels/groups across accounts
  - Dry-run preview: show what would be done without executing
  - Operation queue: view status of queued operations from DB
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import MassOpCb, BmCb
from bot.states import MassPublishFSM, BulkBotEditFSM, BulkJoinFSM, BulkLeaveFSM, OpBuilderFSM
from bot.utils.op_helpers import _acc_label, _get_active_accounts, _progress_bar

log = logging.getLogger(__name__)
router = Router()


# ── Timing options ──────────────────────────────────────────────────────────

_TIMING_OPTIONS = [
    ("⚡ Немедленно",      "0"),
    ("⏱ Задержка 5с",    "5"),
    ("⏳ Задержка 30с",   "30"),
]

_TARGET_LABELS = {
    "channels": "Каналы",
    "groups":   "Группы",
    "both":     "Каналы и группы",
}

_FILTER_LABELS = {
    "all":     "Все активные аккаунты",
    "account": "По аккаунту",
    "cluster": "По кластеру",
}


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
    return kb


# ── Main menu ────────────────────────────────────────────────────────────────

@router.callback_query(MassOpCb.filter(F.action == "menu"))
async def cb_mass_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠️ Построитель операций",         callback_data=MassOpCb(action="build"))
    kb.button(text="📤 Массовая публикация",           callback_data=MassOpCb(action="mass_publish"))
    kb.button(text="🔗 Массовый join каналов",         callback_data=MassOpCb(action="bulk_join"))
    kb.button(text="🚪 Массовый выход из каналов",     callback_data=MassOpCb(action="bulk_leave"))
    kb.button(text="✏️ Массовое редактирование ботов", callback_data=MassOpCb(action="bulk_bot_edit"))
    kb.button(text="🔍 Предпросмотр (Dry Run)",        callback_data=MassOpCb(action="dry_run"))
    kb.button(text="📋 Очередь операций",              callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Назад",                         callback_data=BmCb(action="operations"))
    kb.adjust(2, 2, 2, 1, 1)
    await callback.message.edit_text(
        "🛠️ <b>Построитель операций</b>\n\n"
        "🛠️ <b>Построитель</b> — пошаговый wizard для создания любой операции\n"
        "📤 <b>Публикация</b> — отправить пост во все каналы\n"
        "🔗 <b>Join</b> — вступить в список каналов/групп несколькими аккаунтами\n"
        "🚪 <b>Leave</b> — выйти из каналов/групп несколькими аккаунтами\n"
        "✏️ <b>Редактирование ботов</b> — изменить имя/описание всех ботов сразу\n\n"
        "Выберите тип операции:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MASS PUBLISH
# ══════════════════════════════════════════════════════════════════════════

# Step 1: choose target type

@router.callback_query(MassOpCb.filter(F.action == "mass_publish"))
async def cb_mass_publish_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            "🔒 <b>Массовая публикация — PRO+</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    await state.set_state(MassPublishFSM.choosing_targets)
    await state.update_data(mp_step="targets")

    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Каналы",              callback_data=MassOpCb(action="mp_target", op_type="channels"))
    kb.button(text="👥 Группы",              callback_data=MassOpCb(action="mp_target", op_type="groups"))
    kb.button(text="📢+👥 Каналы и группы", callback_data=MassOpCb(action="mp_target", op_type="both"))
    kb.button(text="❌ Отмена",              callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 1, 1)
    await callback.message.edit_text(
        "📤 <b>Массовая публикация</b>\n\nШаг 1 из 5: Выберите тип целей:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# Step 2: choose filter (all / by account / by cluster)

@router.callback_query(MassOpCb.filter(F.action == "mp_target"))
async def cb_mp_target_chosen(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext
) -> None:
    await callback.answer()
    _op_type = callback_data.op_type or ""
    await state.update_data(mp_target=_op_type)
    await state.set_state(MassPublishFSM.choosing_selector)

    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Все активные аккаунты", callback_data=MassOpCb(action="mp_filter", op_type="all"))
    kb.button(text="👤 По аккаунту",           callback_data=MassOpCb(action="mp_filter", op_type="account"))
    kb.button(text="🗂 По кластеру",           callback_data=MassOpCb(action="mp_filter", op_type="cluster"))
    kb.button(text="❌ Отмена",                callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    target_label = _TARGET_LABELS.get(_op_type, _op_type)
    await callback.message.edit_text(
        f"📤 <b>Массовая публикация</b>\n"
        f"Цели: <b>{target_label}</b>\n\n"
        "Шаг 2 из 5: Выберите фильтр аккаунтов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# Step 2b: filter by account — show account list

@router.callback_query(MassOpCb.filter(F.action == "mp_filter"))
async def cb_mp_filter_chosen(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    filter_type = callback_data.op_type or ""
    await state.update_data(mp_filter=filter_type)
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")

    if filter_type == "account":
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
                text=f"👤 {_acc_label(acc)}",
                callback_data=MassOpCb(action="mp_acc_pick", op_id=acc["id"]),
            )
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            f"📤 <b>Массовая публикация</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 2б: Выберите аккаунт:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    if filter_type == "cluster":
        # Fetch distinct clusters from tg_accounts
        rows = await pool.fetch(
            "SELECT DISTINCT cluster FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND cluster IS NOT NULL",
            callback.from_user.id,
        )
        clusters = [r["cluster"] for r in rows if r["cluster"]]
        if not clusters:
            # No clusters defined — fall back to "all"
            await state.update_data(mp_filter="all", mp_acc_id=None, mp_cluster=None)
            await _ask_mp_text(callback.message, state, target_label, edit=True)
            return
        kb = InlineKeyboardBuilder()
        for cl in clusters:
            kb.button(
                text=f"🗂 {cl}",
                callback_data=MassOpCb(action="mp_cluster_pick", op_type=cl[:40]),
            )
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            f"📤 <b>Массовая публикация</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 2б: Выберите кластер:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # filter_type == "all"
    await state.update_data(mp_acc_id=None, mp_cluster=None)
    await _ask_mp_text(callback.message, state, target_label, edit=True)


@router.callback_query(MassOpCb.filter(F.action == "mp_acc_pick"))
async def cb_mp_acc_picked(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(mp_acc_id=callback_data.op_id, mp_cluster=None)
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")
    await _ask_mp_text(callback.message, state, target_label, edit=True)


@router.callback_query(MassOpCb.filter(F.action == "mp_cluster_pick"))
async def cb_mp_cluster_picked(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(mp_cluster=callback_data.op_type or "", mp_acc_id=None)
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")
    await _ask_mp_text(callback.message, state, target_label, edit=True)


# Step 3: enter text

async def _ask_mp_text(msg, state: FSMContext, target_label: str, edit: bool = False) -> None:
    await state.set_state(MassPublishFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    text = (
        f"📤 <b>Массовая публикация</b>\n"
        f"Цели: <b>{target_label}</b>\n\n"
        "Шаг 3 из 5: Введите текст поста (поддерживается HTML):"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(MassPublishFSM.waiting_text)
async def fsm_mp_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите текст поста:")
        return
    await state.update_data(mp_text=text)
    await state.set_state(MassPublishFSM.choosing_timing)

    kb = InlineKeyboardBuilder()
    for label, val in _TIMING_OPTIONS:
        kb.button(text=label, callback_data=MassOpCb(action="mp_timing", op_type=val))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📤 <b>Массовая публикация</b>\n\nШаг 4 из 5: Выберите задержку между постами:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# Step 4: choose timing

@router.callback_query(MassOpCb.filter(F.action == "mp_timing"))
async def cb_mp_timing(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    delay = int(callback_data.op_type or "0")
    await state.update_data(mp_delay=delay)
    await state.set_state(MassPublishFSM.previewing)

    data = await state.get_data()
    target = data.get("mp_target", "channels")
    filter_type = data.get("mp_filter", "all")
    mp_acc_id = data.get("mp_acc_id")
    mp_cluster = data.get("mp_cluster")
    mp_text = data.get("mp_text", "")

    # Count channels for preview
    channel_count = await _count_targets(pool, callback.from_user.id, target, filter_type, mp_acc_id, mp_cluster)

    target_label = _TARGET_LABELS.get(target, target)
    filter_label = _FILTER_LABELS.get(filter_type, filter_type)
    if filter_type == "account" and mp_acc_id:
        acc_row = await pool.fetchrow("SELECT first_name, phone FROM tg_accounts WHERE id=$1", mp_acc_id)
        if acc_row:
            filter_label = f"Аккаунт: {acc_row['first_name'] or acc_row['phone']}"
    elif filter_type == "cluster" and mp_cluster:
        filter_label = f"Кластер: {mp_cluster}"

    delay_label = f"{delay}с" if delay > 0 else "Немедленно"
    estimated_mins = round(channel_count * delay / 60, 1) if delay else 0
    preview_text = html.escape(mp_text[:300])

    await state.set_state(MassPublishFSM.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить",  callback_data=MassOpCb(action="mp_confirm"))
    kb.button(text="🔍 Dry Run",   callback_data=MassOpCb(action="dry_run"))
    kb.button(text="❌ Отмена",    callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 1)
    await callback.message.edit_text(
        f"📤 <b>Предпросмотр публикации</b>\n\n"
        f"Тип целей: <b>{target_label}</b>\n"
        f"Фильтр: <b>{filter_label}</b>\n"
        f"Найдено: <b>{channel_count}</b> каналов/групп\n"
        f"Задержка: <b>{delay_label}</b>\n"
        f"Расчётное время: ~{estimated_mins} мин\n\n"
        f"Текст:\n<i>{preview_text}</i>\n\n"
        "Подтвердить запуск?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# Step 5: confirm and run

@router.callback_query(MassOpCb.filter(F.action == "mp_confirm"))
async def cb_mp_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю рассылку...")
    data = await state.get_data()
    await state.clear()

    target = data.get("mp_target", "channels")
    filter_type = data.get("mp_filter", "all")
    mp_acc_id = data.get("mp_acc_id")
    mp_cluster = data.get("mp_cluster")
    mp_text = data.get("mp_text", "")
    delay = int(data.get("mp_delay", 0))

    # Build list of (account, dialog) to post to
    accounts = await _get_accounts_for_filter(pool, callback.from_user.id, filter_type, mp_acc_id, mp_cluster)

    from services import account_manager

    targets_with_dialogs: list[tuple] = []
    for acc in accounts:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc)
        for d in (dialogs or []):
            if _dialog_matches_target(d, target):
                targets_with_dialogs.append((acc, d))

    total = len(targets_with_dialogs)
    if total == 0:
        await callback.message.edit_text(
            "⚠️ Нет подходящих каналов/групп для рассылки.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    # Log operation to DB if table exists
    op_id = await _create_op_record(pool, callback.from_user.id, "mass_publish", total, {
        "target": target, "filter": filter_type, "delay": delay,
    })

    ok_count, err_count = 0, 0
    progress_msg = await callback.message.edit_text(
        f"⏳ Рассылка... 0/{total}\n[{'░' * 10}] 0%",
        parse_mode="HTML",
    )

    for idx, (acc, dialog) in enumerate(targets_with_dialogs, 1):
        access_hash = dialog.get("access_hash", 0) or 0
        try:
            result = await account_manager.post_to_channel(
                acc["session_str"],
                dialog["id"],
                mp_text,
                access_hash=access_hash,
                _acc=acc,
            )
            if "error" in result or result.get("banned"):
                err_count += 1
                step_status = "error"
            else:
                ok_count += 1
                step_status = "ok"
        except Exception as e:
            err_count += 1
            step_status = "error"

        if op_id:
            await _log_op_step(pool, op_id, idx, str(dialog["id"]), step_status)

        bar = _progress_bar(idx, total)
        pct = round(100 * idx / total)
        try:
            await progress_msg.edit_text(
                f"⏳ Рассылка... {idx}/{total}\n[{bar}] {pct}%\n✅ {ok_count} ❌ {err_count}",
                parse_mode="HTML",
            )
        except Exception:
            pass

        if delay > 0:
            await asyncio.sleep(delay)

    if op_id:
        await _finish_op_record(pool, op_id, ok_count, err_count)

    await progress_msg.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Всего: {total} · ✅ {ok_count} · ❌ {err_count}",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "cancel_op"))
async def cb_cancel_op(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    # Cancel both pending and running operations
    result = await pool.execute(
        "UPDATE operation_queue SET status='cancelled', finished_at=now() "
        "WHERE id=$1 AND owner_id=$2 AND status IN ('pending','running')",
        callback_data.op_id, callback.from_user.id,
    )
    if result == "UPDATE 0":
        await callback.answer("Операция уже завершена или не найдена.", show_alert=True)
        return
    # Refresh queue view
    rows = await pool.fetch(
        "SELECT id, op_type, status, done_items, total_items, created_at "
        "FROM operation_queue "
        "WHERE owner_id=$1 "
        "ORDER BY created_at DESC LIMIT 10",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Назад",   callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    if not rows:
        await callback.message.edit_text(
            "📋 <b>Очередь операций</b>\n\nОчередь пуста.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    _STATUS_ICONS = {
        "pending":   "⏳",
        "running":   "🔄",
        "done":      "✅",
        "failed":    "❌",
        "cancelled": "🚫",
    }
    lines = ["📋 <b>Очередь операций</b>\n"]
    for i, r in enumerate(rows, 1):
        icon = _STATUS_ICONS.get(r["status"], "❓")
        op_type = html.escape(r["op_type"])
        status = r["status"]
        done = r["done_items"] or 0
        total = r["total_items"] or 0
        created = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "—"
        if status == "running":
            progress = f"{done}/{total} ✓"
        elif status == "done":
            progress = created
        else:
            progress = f"{total} элементов"
        lines.append(f"{i}. {op_type} | {icon} {status} | {progress}")
        if status in ("pending", "running"):
            kb.button(text=f"❌ Отменить #{r['id']}", callback_data=MassOpCb(action="cancel_op", op_id=r["id"]))
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "retry_op"))
async def cb_retry_op(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    result = await pool.execute(
        "UPDATE operation_queue SET status='pending', last_error=NULL, retry_count=0, "
        "finished_at=NULL WHERE id=$1 AND owner_id=$2 AND status='failed'",
        callback_data.op_id, callback.from_user.id,
    )
    if result == "UPDATE 0":
        await callback.answer("Операция не найдена или уже выполнена.", show_alert=True)
        return
    # Re-render queue view
    try:
        rows = await pool.fetch(
            "SELECT id, op_type, status, done_items, total_items, created_at, "
            "last_error, retry_count, max_retries, finished_at "
            "FROM operation_queue "
            "WHERE owner_id=$1 "
            "ORDER BY created_at DESC LIMIT 10",
            callback.from_user.id,
        )
    except Exception as e:
        log.warning("Queue fetch error after retry: %s", e)
        rows = []
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Назад",   callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    if not rows:
        await callback.message.edit_text(
            "📋 <b>Очередь операций</b>\n\nОчередь пуста.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    _STATUS_ICONS = {
        "pending":   "⏳",
        "running":   "🔄",
        "done":      "✅",
        "failed":    "❌",
        "cancelled": "🚫",
    }
    lines = ["📋 <b>Очередь операций</b>\n"]
    for i, r in enumerate(rows, 1):
        icon = _STATUS_ICONS.get(r["status"], "❓")
        op_type = html.escape(r["op_type"])
        status = r["status"]
        done = r["done_items"] or 0
        total = r["total_items"] or 0
        created = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "—"
        retry_count = r["retry_count"] or 0
        max_retries = r["max_retries"] or 0
        last_error = r["last_error"] or ""
        if status == "running":
            progress = f"{done}/{total} ✓"
        elif status == "done":
            progress = created
        elif status == "failed":
            progress = f"{total} элементов (попытка {retry_count}/{max_retries})"
        else:
            progress = f"{total} элементов"
        lines.append(f"{i}. {op_type} | {icon} {status} | {progress}")
        if status == "failed" and last_error:
            err_preview = html.escape(last_error[:60])
            lines.append(f"   ⚠️ <i>{err_preview}</i>")
        if status in ("pending", "running"):
            kb.button(text=f"❌ Отменить #{r['id']}", callback_data=MassOpCb(action="cancel_op", op_id=r["id"]))
        elif status == "failed":
            kb.button(text=f"🔁 Повторить #{r['id']}", callback_data=MassOpCb(action="retry_op", op_id=r["id"]))
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# DRY RUN PREVIEW
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassOpCb.filter(F.action == "dry_run"))
async def cb_dry_run(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()

    target = data.get("mp_target", "channels")
    filter_type = data.get("mp_filter", "all")
    mp_acc_id = data.get("mp_acc_id")
    mp_cluster = data.get("mp_cluster")
    delay = int(data.get("mp_delay", 30))
    mp_text = data.get("mp_text", "")

    target_label = _TARGET_LABELS.get(target, "Каналы")
    filter_label = _FILTER_LABELS.get(filter_type, "Все активные аккаунты")

    channel_count = await _count_targets(pool, callback.from_user.id, target, filter_type, mp_acc_id, mp_cluster)
    estimated_mins = round(channel_count * delay / 60, 1) if delay else 0
    delay_label = f"{delay}с" if delay > 0 else "Немедленно"

    kb = InlineKeyboardBuilder()
    if mp_text:
        kb.button(text="✅ Запустить", callback_data=MassOpCb(action="mp_confirm"))
    else:
        kb.button(text="📤 Настроить публикацию", callback_data=MassOpCb(action="mass_publish"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"🔍 <b>Предпросмотр операции</b>\n\n"
        f"Тип: Публикация в {target_label.lower()}\n"
        f"Фильтр: {filter_label}\n"
        f"Каналов найдено: <b>{channel_count}</b>\n"
        f"Расчётное время: ~{estimated_mins} мин\n"
        f"Задержка между постами: {delay_label}\n\n"
        f"<i>Операция ещё не выполнена — это только предпросмотр.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# OPERATION QUEUE
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassOpCb.filter(F.action == "queue"))
async def cb_queue(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()

    try:
        rows = await pool.fetch(
            "SELECT id, op_type, status, done_items, total_items, created_at, "
            "last_error, retry_count, max_retries, finished_at "
            "FROM operation_queue "
            "WHERE owner_id=$1 "
            "ORDER BY created_at DESC LIMIT 10",
            callback.from_user.id,
        )
    except asyncpg.exceptions.UndefinedTableError:
        rows = []
    except Exception as e:
        log.warning("Queue fetch error: %s", e)
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Назад",   callback_data=MassOpCb(action="menu"))
    kb.adjust(2)

    if not rows:
        await callback.message.edit_text(
            "📋 <b>Очередь операций</b>\n\nОчередь пуста.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    _STATUS_ICONS = {
        "pending":   "⏳",
        "running":   "🔄",
        "done":      "✅",
        "failed":    "❌",
        "cancelled": "🚫",
    }
    lines = ["📋 <b>Очередь операций</b>\n"]
    for i, r in enumerate(rows, 1):
        icon = _STATUS_ICONS.get(r["status"], "❓")
        op_type = html.escape(r["op_type"])
        status = r["status"]
        done = r["done_items"] or 0
        total = r["total_items"] or 0
        created = r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "—"
        retry_count = r["retry_count"] or 0
        max_retries = r["max_retries"] or 0
        last_error = r["last_error"] or ""

        if status == "running":
            progress = f"{done}/{total} ✓"
        elif status == "done":
            progress = created
        elif status == "failed":
            progress = f"{total} элементов (попытка {retry_count}/{max_retries})"
        else:
            progress = f"{total} элементов"

        lines.append(f"{i}. {op_type} | {icon} {status} | {progress}")

        if status == "failed" and last_error:
            err_preview = html.escape(last_error[:60])
            lines.append(f"   ⚠️ <i>{err_preview}</i>")

        if status in ("pending", "running"):
            kb.button(text=f"❌ Отменить #{r['id']}", callback_data=MassOpCb(action="cancel_op", op_id=r["id"]))
        elif status == "failed":
            kb.button(text=f"🔁 Повторить #{r['id']}", callback_data=MassOpCb(action="retry_op", op_id=r["id"]))

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK BOT EDIT
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassOpCb.filter(F.action == "bulk_bot_edit"))
async def cb_bulk_bot_edit_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from bot.utils.subscription import require_plan
    from bot.keyboards import subscription_locked_markup
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            "🔒 <b>Массовое редактирование ботов — PRO</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="operations")),
        )
        return
    await callback.answer()
    await state.set_state(BulkBotEditFSM.choosing_field)

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя бота",        callback_data=MassOpCb(action="bbe_field", op_type="name"))
    kb.button(text="📄 Описание",        callback_data=MassOpCb(action="bbe_field", op_type="desc"))
    kb.button(text="📝 Краткое описание", callback_data=MassOpCb(action="bbe_field", op_type="short_desc"))
    kb.button(text="⌨️ Команды",         callback_data=MassOpCb(action="bbe_field", op_type="commands"))
    kb.button(text="❌ Отмена",          callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        "✏️ <b>Массовое редактирование ботов</b>\n\nВыберите поле для изменения:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bbe_field"))
async def cb_bbe_field_chosen(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext
) -> None:
    await callback.answer()
    field = callback_data.op_type or ""
    await state.update_data(bbe_field=field)
    await state.set_state(BulkBotEditFSM.waiting_value)

    _FIELD_LABELS = {
        "name":       "имя бота",
        "desc":       "описание",
        "short_desc": "краткое описание",
        "commands":   "команды (формат: /cmd - описание)",
    }
    field_label = _FIELD_LABELS.get(field, field)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="bulk_bot_edit"))
    await callback.message.edit_text(
        f"✏️ <b>Массовое редактирование</b>\n\nВведите новое <b>{field_label}</b> для всех ботов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkBotEditFSM.waiting_value)
async def fsm_bbe_value(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠️ Введите значение:")
        return
    await state.update_data(bbe_value=value)
    await state.set_state(BulkBotEditFSM.previewing)

    data = await state.get_data()
    field = data.get("bbe_field", "")
    preview = html.escape(value[:300])

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить ко всем ботам", callback_data=MassOpCb(action="bbe_confirm"))
    kb.button(text="❌ Отмена",                  callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✏️ <b>Предпросмотр изменения</b>\n\n"
        f"Поле: <b>{field}</b>\n"
        f"Новое значение:\n<i>{preview}</i>\n\n"
        "Это будет применено ко <b>всем вашим ботам</b>. Продолжить?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bbe_confirm"))
async def cb_bbe_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Применяю...")
    data = await state.get_data()
    await state.clear()

    field = data.get("bbe_field", "")
    value = data.get("bbe_value", "")

    # Fetch all bots for this user
    bots = await pool.fetch(
        "SELECT id, token FROM bots WHERE owner_id=$1",
        callback.from_user.id,
    )
    if not bots:
        await callback.message.edit_text(
            "⚠️ У вас нет добавленных ботов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    total = len(bots)
    ok_count, err_count = 0, 0
    progress_msg = await callback.message.edit_text(
        f"⏳ Редактирую ботов... 0/{total}",
        parse_mode="HTML",
    )

    import aiohttp
    for idx, bot in enumerate(bots, 1):
        token = bot["token"]
        try:
            async with aiohttp.ClientSession() as session:
                if field == "name":
                    url = f"https://api.telegram.org/bot{token}/setMyName"
                    payload = {"name": value}
                elif field == "desc":
                    url = f"https://api.telegram.org/bot{token}/setMyDescription"
                    payload = {"description": value}
                elif field == "short_desc":
                    url = f"https://api.telegram.org/bot{token}/setMyShortDescription"
                    payload = {"short_description": value}
                elif field == "commands":
                    # Parse commands: "/cmd - description" format
                    commands = []
                    for line in value.strip().splitlines():
                        line = line.strip()
                        if " - " in line:
                            cmd_part, desc_part = line.split(" - ", 1)
                            cmd = cmd_part.strip().lstrip("/")
                            if cmd:
                                commands.append({"command": cmd, "description": desc_part.strip()[:256]})
                    url = f"https://api.telegram.org/bot{token}/setMyCommands"
                    payload = {"commands": json.dumps(commands)}
                else:
                    err_count += 1
                    continue

                async with session.post(url, data=payload) as resp:
                    result = await resp.json()
                    if result.get("ok"):
                        ok_count += 1
                    else:
                        err_count += 1
        except Exception:
            err_count += 1

        try:
            await progress_msg.edit_text(
                f"⏳ Редактирую ботов... {idx}/{total}\n✅ {ok_count} ❌ {err_count}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await asyncio.sleep(1)

    await progress_msg.edit_text(
        f"✅ <b>Массовое редактирование завершено</b>\n\n"
        f"Всего ботов: {total}\n"
        f"Успешно: {ok_count}\n"
        f"Ошибок: {err_count}",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _dialog_matches_target(dialog: dict, target: str) -> bool:
    dtype = dialog.get("type", "")
    if target == "channels":
        return dtype == "channel"
    if target == "groups":
        return dtype in ("megagroup", "supergroup", "group", "chat")
    # "both"
    return dtype in ("channel", "megagroup", "supergroup", "group", "chat")


async def _get_accounts_for_filter(
    pool: asyncpg.Pool, owner_id: int,
    filter_type: str, acc_id: int | None, cluster: str | None,
) -> list[asyncpg.Record]:
    if filter_type == "account" and acc_id:
        return await pool.fetch(
            "SELECT id, session_str, first_name, phone FROM tg_accounts "
            "WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            acc_id, owner_id,
        )
    if filter_type == "cluster" and cluster:
        return await pool.fetch(
            "SELECT id, session_str, first_name, phone FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND cluster=$2",
            owner_id, cluster,
        )
    return await pool.fetch(
        "SELECT id, session_str, first_name, phone FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE",
        owner_id,
    )


async def _count_targets(
    pool: asyncpg.Pool, owner_id: int,
    target: str, filter_type: str, acc_id: int | None, cluster: str | None,
) -> int:
    """Count number of matching dialogs without fetching all messages."""
    accounts = await _get_accounts_for_filter(pool, owner_id, filter_type, acc_id, cluster)
    if not accounts:
        return 0
    # For preview purposes, estimate 3 dialogs per account to avoid long delays
    # A real count would require fetching all dialogs from Telethon
    return len(accounts) * 3


async def _create_op_record(
    pool: asyncpg.Pool, owner_id: int, op_type: str, total_items: int, params: dict
) -> int | None:
    try:
        row = await pool.fetchrow(
            "INSERT INTO operation_queue (owner_id, op_type, status, total_items, params) "
            "VALUES ($1, $2, 'running', $3, $4) RETURNING id",
            owner_id, op_type, total_items, json.dumps(params),
        )
        return row["id"] if row else None
    except Exception as e:
        log.warning("Could not create op record: %s", e)
        return None


async def _log_op_step(
    pool: asyncpg.Pool, op_id: int, step_num: int, target: str, status: str
) -> None:
    try:
        await pool.execute(
            "INSERT INTO operation_log (op_id, step_num, target, status) VALUES ($1, $2, $3, $4)",
            op_id, step_num, target, status,
        )
    except Exception:
        pass


async def _finish_op_record(
    pool: asyncpg.Pool, op_id: int, ok_count: int, err_count: int
) -> None:
    try:
        status = "done" if err_count == 0 else ("failed" if ok_count == 0 else "done")
        await pool.execute(
            "UPDATE operation_queue SET status=$1, done_items=$2, finished_at=now() WHERE id=$3",
            status, ok_count, op_id,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# BULK JOIN WIZARD
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassOpCb.filter(F.action == "bulk_join"))
async def cb_bulk_join_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            "🔒 <b>Массовый join — PRO+</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await state.set_state(BulkJoinFSM.waiting_links)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    await callback.message.edit_text(
        "🔗 <b>Массовый join — Шаг 1/3</b>\n\n"
        "Введите ссылки или юзернеймы каналов/групп — <b>по одному на строку</b>:\n\n"
        "<code>@channel_name\n"
        "https://t.me/channel_name\n"
        "https://t.me/+InviteHash</code>\n\n"
        "Поддерживаются публичные каналы, группы и приватные ссылки-инвайты.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkJoinFSM.waiting_links)
async def fsm_bulk_join_links(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = message.text or ""
    links = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not links:
        await message.answer("⚠️ Введите хотя бы одну ссылку или юзернейм:")
        return
    if len(links) > 50:
        await message.answer("⚠️ Максимум 50 ссылок за одну операцию.")
        return

    await state.update_data(bj_links=links)
    await state.set_state(BulkJoinFSM.choosing_accounts)

    accounts = await _get_active_accounts(pool, message.from_user.id)
    if not accounts:
        await state.clear()
        await message.answer(
            "⚠️ Нет активных аккаунтов. Добавьте через /accounts.",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Все активные аккаунты", callback_data=MassOpCb(action="bj_accs", op_type="all"))
    for acc in accounts[:10]:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=MassOpCb(action="bj_accs", op_id=acc["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🔗 <b>Массовый join — Шаг 2/3</b>\n\n"
        f"Каналов/групп: <b>{len(links)}</b>\n\n"
        "Выберите аккаунты для вступления:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bj_accs"))
async def cb_bulk_join_accs(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    if not links:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    uid = callback.from_user.id
    if callback_data.op_type == "all":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        acc_label = f"все ({len(acc_ids)})"
    else:
        acc_ids = [callback_data.op_id]
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.op_id, uid,
        )
        acc_label = acc["phone"] if acc else f"id{callback_data.op_id}"

    if not acc_ids:
        await callback.answer("Нет активных аккаунтов", show_alert=True)
        return

    # Show preview + delay selector
    link_preview = "\n".join(f"• {html.escape(ln)}" for ln in links[:5])
    if len(links) > 5:
        link_preview += f"\n… и ещё {len(links) - 5}"

    await state.update_data(bj_acc_ids=acc_ids, bj_acc_label=acc_label)

    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Быстро (5-15с)",    callback_data=MassOpCb(action="bj_delay", op_type="fast"))
    kb.button(text="🛡 Нормально (30-60с)", callback_data=MassOpCb(action="bj_delay", op_type="normal"))
    kb.button(text="🐌 Медленно (60-120с)", callback_data=MassOpCb(action="bj_delay", op_type="slow"))
    kb.button(text="🧠 Умный (авто)",       callback_data=MassOpCb(action="bj_delay", op_type="smart"))
    kb.button(text="❌ Отмена",             callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        f"🔗 <b>Массовый join — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(links)}</b>\n\n"
        f"<b>Список:</b>\n{link_preview}\n\n"
        f"Выберите режим задержки между вступлениями:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


_DELAY_LABELS = {
    "fast":   ("⚡ Быстро",    "5–15с",   "1–3 мин"),
    "normal": ("🛡 Нормально", "30–60с",  "5–15 мин"),
    "slow":   ("🐌 Медленно",  "60–120с", "10–30 мин"),
    "smart":  ("🧠 Умный",     "авто",    "переменно"),
}


@router.callback_query(MassOpCb.filter(F.action == "bj_delay"))
async def cb_bulk_join_delay(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    acc_ids = sd.get("bj_acc_ids", [])
    acc_label = sd.get("bj_acc_label", "?")

    delay_mode = callback_data.op_type or "smart"
    await state.update_data(bj_delay_mode=delay_mode)

    link_preview = "\n".join(f"• {html.escape(ln)}" for ln in links[:5])
    if len(links) > 5:
        link_preview += f"\n… и ещё {len(links) - 5}"

    icon, delay_str, time_est = _DELAY_LABELS.get(delay_mode, ("🧠 Умный", "авто", "переменно"))
    n = len(links) * len(acc_ids)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить join", callback_data=MassOpCb(action="bj_confirm"))
    kb.button(text="◀️ Изменить задержку", callback_data=MassOpCb(action="bj_accs", op_type="reselect"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🔗 <b>Массовый join — Шаг 4/4 (Подтверждение)</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(links)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n"
        f"Операций: <b>{n}</b> (~{time_est})\n\n"
        f"<b>Список:</b>\n{link_preview}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bj_confirm"))
async def cb_bulk_join_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    acc_ids = sd.get("bj_acc_ids", [])
    delay_mode = sd.get("bj_delay_mode", "smart")

    if not links or not acc_ids:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    params = {"links": links, "account_ids": acc_ids, "delay_mode": delay_mode}
    try:
        op_id = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id, op_type, status, params, total_items)
               VALUES($1, 'bulk_join', 'pending', $2::jsonb, $3)
               RETURNING id""",
            callback.from_user.id,
            json.dumps(params),
            len(links) * len(acc_ids),
        )
    except Exception as e:
        log.error("bulk_join confirm error: %s", e)
        await callback.answer("Ошибка создания операции", show_alert=True)
        return

    icon, delay_str, _ = _DELAY_LABELS.get(delay_mode, ("🧠 Умный", "авто", ""))
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Меню", callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: 🔗 Массовый join\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Каналов: <b>{len(links)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n\n"
        f"Воркер запустит её автоматически.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# BULK LEAVE WIZARD
# ══════════════════════════════════════════════════════════════════════════

@router.callback_query(MassOpCb.filter(F.action == "bulk_leave"))
async def cb_bulk_leave_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            "🔒 <b>Массовый leave — PRO+</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await state.set_state(BulkLeaveFSM.waiting_channels)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    await callback.message.edit_text(
        "🚪 <b>Массовый leave — Шаг 1/3</b>\n\n"
        "Введите юзернеймы или ID каналов/групп — <b>по одному на строку</b>:\n\n"
        "<code>@channel_name\n"
        "-1001234567890\n"
        "username</code>\n\n"
        "Аккаунты выйдут из всех указанных каналов.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkLeaveFSM.waiting_channels)
async def fsm_bulk_leave_channels(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = message.text or ""
    channels = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not channels:
        await message.answer("⚠️ Введите хотя бы один юзернейм или ID канала:")
        return
    if len(channels) > 50:
        await message.answer("⚠️ Максимум 50 каналов за одну операцию.")
        return

    await state.update_data(bl_channels=channels)
    await state.set_state(BulkLeaveFSM.choosing_accounts)

    accounts = await _get_active_accounts(pool, message.from_user.id)
    if not accounts:
        await state.clear()
        await message.answer("⚠️ Нет активных аккаунтов. Добавьте через /accounts.")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Все активные аккаунты", callback_data=MassOpCb(action="bl_accs", op_type="all"))
    for acc in accounts[:10]:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=MassOpCb(action="bl_accs", op_id=acc["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🚪 <b>Массовый leave — Шаг 2/3</b>\n\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n\n"
        "Выберите аккаунты для выхода:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bl_accs"))
async def cb_bulk_leave_accs(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    if not channels:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    uid = callback.from_user.id
    if callback_data.op_type == "all":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        acc_label = f"все ({len(acc_ids)})"
    else:
        acc_ids = [callback_data.op_id]
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            callback_data.op_id, uid,
        )
        acc_label = acc["phone"] if acc else f"id{callback_data.op_id}"

    if not acc_ids:
        await callback.answer("Нет активных аккаунтов", show_alert=True)
        return

    ch_preview = "\n".join(f"• {html.escape(ch)}" for ch in channels[:5])
    if len(channels) > 5:
        ch_preview += f"\n… и ещё {len(channels) - 5}"

    await state.update_data(bl_acc_ids=acc_ids, bl_acc_label=acc_label)

    kb = InlineKeyboardBuilder()
    kb.button(text="⚡ Быстро (5-15с)",    callback_data=MassOpCb(action="bl_delay", op_type="fast"))
    kb.button(text="🛡 Нормально (15-45с)", callback_data=MassOpCb(action="bl_delay", op_type="normal"))
    kb.button(text="🐌 Медленно (60-120с)", callback_data=MassOpCb(action="bl_delay", op_type="slow"))
    kb.button(text="🧠 Умный (авто)",       callback_data=MassOpCb(action="bl_delay", op_type="smart"))
    kb.button(text="❌ Отмена",             callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        f"🚪 <b>Массовый leave — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n\n"
        f"<b>Список:</b>\n{ch_preview}\n\n"
        f"Выберите режим задержки между выходами:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


_DELAY_LABELS_LEAVE = {
    "fast":   ("⚡ Быстро",    "5–15с",   "1–2 мин"),
    "normal": ("🛡 Нормально", "15–45с",  "3–10 мин"),
    "slow":   ("🐌 Медленно",  "60–120с", "10–30 мин"),
    "smart":  ("🧠 Умный",     "авто",    "переменно"),
}


@router.callback_query(MassOpCb.filter(F.action == "bl_delay"))
async def cb_bulk_leave_delay(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    acc_ids = sd.get("bl_acc_ids", [])
    acc_label = sd.get("bl_acc_label", "?")

    delay_mode = callback_data.op_type or "smart"
    await state.update_data(bl_delay_mode=delay_mode)

    ch_preview = "\n".join(f"• {html.escape(ch)}" for ch in channels[:5])
    if len(channels) > 5:
        ch_preview += f"\n… и ещё {len(channels) - 5}"

    icon, delay_str, time_est = _DELAY_LABELS_LEAVE.get(delay_mode, ("🧠 Умный", "авто", "переменно"))
    n = len(channels) * len(acc_ids)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить leave", callback_data=MassOpCb(action="bl_confirm"))
    kb.button(text="◀️ Изменить задержку", callback_data=MassOpCb(action="bl_accs", op_type="reselect"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🚪 <b>Массовый leave — Шаг 4/4 (Подтверждение)</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n"
        f"Операций: <b>{n}</b> (~{time_est})\n\n"
        f"<b>Список:</b>\n{ch_preview}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bl_confirm"))
async def cb_bulk_leave_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    acc_ids = sd.get("bl_acc_ids", [])
    delay_mode = sd.get("bl_delay_mode", "smart")

    if not channels or not acc_ids:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    params = {"channels": channels, "account_ids": acc_ids, "delay_mode": delay_mode}
    try:
        op_id = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id, op_type, status, params, total_items)
               VALUES($1, 'bulk_leave', 'pending', $2::jsonb, $3)
               RETURNING id""",
            callback.from_user.id,
            json.dumps(params),
            len(channels) * len(acc_ids),
        )
    except Exception as e:
        log.error("bulk_leave confirm error: %s", e)
        await callback.answer("Ошибка создания операции", show_alert=True)
        return

    icon, delay_str, _ = _DELAY_LABELS_LEAVE.get(delay_mode, ("🧠 Умный", "авто", ""))
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Меню", callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: 🚪 Массовый leave\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n\n"
        f"Воркер запустит её автоматически.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# OPERATION BUILDER FSM WIZARD
# Пошаговый мастер: тип → цели → параметры → preview → подтверждение
# ══════════════════════════════════════════════════════════════════════════

_OP_TYPE_META = {
    "mass_publish": {
        "icon": "📤",
        "label": "Массовая публикация",
        "desc": "Отправить пост во все каналы/группы",
        "plan": "starter",
    },
    "bulk_join": {
        "icon": "🔗",
        "label": "Массовый join",
        "desc": "Вступить в каналы/группы несколькими аккаунтами",
        "plan": "starter",
    },
    "bulk_leave": {
        "icon": "🚪",
        "label": "Массовый leave",
        "desc": "Выйти из каналов/групп несколькими аккаунтами",
        "plan": "starter",
    },
    "bulk_bot_edit": {
        "icon": "✏️",
        "label": "Массовое редактирование ботов",
        "desc": "Изменить имя/описание/команды всех ботов сразу",
        "plan": "pro",
    },
}


# ── Шаг 1: Выбор типа операции ────────────────────────────────────────────

@router.callback_query(MassOpCb.filter(F.action == "build"))
async def cb_build_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(OpBuilderFSM.choosing_op_type)

    kb = InlineKeyboardBuilder()
    for op_key, meta in _OP_TYPE_META.items():
        kb.button(
            text=f"{meta['icon']} {meta['label']}",
            callback_data=MassOpCb(action="ob_type", op_type=op_key),
        )
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        "🛠️ <b>Построитель операций</b>\n\n"
        "Шаг 1/4: Выберите тип операции\n\n"
        "📤 <b>Публикация</b> — разослать пост по каналам\n"
        "🔗 <b>Join</b> — вступить в каналы (STARTER+)\n"
        "🚪 <b>Leave</b> — выйти из каналов (STARTER+)\n"
        "✏️ <b>Редактирование ботов</b> — обновить профиль ботов (PRO)\n",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Шаг 2: Выбор целей ────────────────────────────────────────────────────

@router.callback_query(MassOpCb.filter(F.action == "ob_type"))
async def cb_ob_type_chosen(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    from bot.utils.subscription import require_plan
    from bot.keyboards import subscription_locked_markup

    op_type = callback_data.op_type or ""
    meta = _OP_TYPE_META.get(op_type)
    if not meta:
        await callback.answer("Неизвестный тип операции", show_alert=True)
        return

    # Проверка подписки
    required_plan = meta["plan"]
    if not await require_plan(pool, callback.from_user.id, required_plan):
        await callback.answer()
        plan_label = "PRO" if required_plan == "pro" else "STARTER+"
        await callback.message.edit_text(
            f"🔒 <b>{meta['label']} — {plan_label}</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(required_plan, back_callback=MassOpCb(action="build")),
        )
        return

    await callback.answer()
    await state.update_data(ob_op_type=op_type)
    await state.set_state(OpBuilderFSM.choosing_targets)

    kb = InlineKeyboardBuilder()

    if op_type == "mass_publish":
        kb.button(text="📢 Каналы",              callback_data=MassOpCb(action="ob_target", op_type="channels"))
        kb.button(text="👥 Группы",              callback_data=MassOpCb(action="ob_target", op_type="groups"))
        kb.button(text="📢+👥 Каналы и группы", callback_data=MassOpCb(action="ob_target", op_type="both"))
        kb.button(text="◀️ Назад",               callback_data=MassOpCb(action="build"))
        kb.adjust(2, 1, 1)
        await callback.message.edit_text(
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            "Шаг 2/4: Выберите тип целей для публикации:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op_type in ("bulk_join", "bulk_leave"):
        # Для join/leave — сразу просим ввести ссылки/каналы
        await state.set_state(OpBuilderFSM.entering_params)
        action_word = "вступления" if op_type == "bulk_join" else "выхода"
        example = (
            "<code>@channel_name\nhttps://t.me/channel_name\nhttps://t.me/+InviteHash</code>"
            if op_type == "bulk_join"
            else "<code>@channel_name\n-1001234567890\nusername</code>"
        )
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="build"))
        await callback.message.edit_text(
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            f"Шаг 2/4: Введите каналы/группы для {action_word} — по одному на строку:\n\n"
            f"{example}\n\n"
            "Максимум 50 записей.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif op_type == "bulk_bot_edit":
        kb.button(text="✏️ Имя бота",         callback_data=MassOpCb(action="ob_target", op_type="name"))
        kb.button(text="📄 Описание",         callback_data=MassOpCb(action="ob_target", op_type="desc"))
        kb.button(text="📝 Краткое описание", callback_data=MassOpCb(action="ob_target", op_type="short_desc"))
        kb.button(text="⌨️ Команды",          callback_data=MassOpCb(action="ob_target", op_type="commands"))
        kb.button(text="◀️ Назад",            callback_data=MassOpCb(action="build"))
        kb.adjust(2, 2, 1)
        await callback.message.edit_text(
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            "Шаг 2/4: Выберите поле для массового редактирования:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── Шаг 2b: Выбор целей mass_publish и bulk_bot_edit ─────────────────────

@router.callback_query(MassOpCb.filter(F.action == "ob_target"))
async def cb_ob_target_chosen(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    target = callback_data.op_type or ""
    await state.update_data(ob_target=target)
    sd = await state.get_data()
    op_type = sd.get("ob_op_type", "")
    meta = _OP_TYPE_META.get(op_type, {})

    await state.set_state(OpBuilderFSM.entering_params)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="ob_type", op_type=op_type))

    if op_type == "mass_publish":
        target_label = _TARGET_LABELS.get(target, target)
        await callback.message.edit_text(
            f"🛠️ <b>Построитель: {meta.get('icon','')} {meta.get('label','')}</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 3/4: Введите текст поста (поддерживается HTML):",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    elif op_type == "bulk_bot_edit":
        _FIELD_LABELS_OB = {
            "name":       "имя бота",
            "desc":       "описание",
            "short_desc": "краткое описание",
            "commands":   "команды (формат: /cmd - описание, по одному на строку)",
        }
        field_label = _FIELD_LABELS_OB.get(target, target)
        await callback.message.edit_text(
            f"🛠️ <b>Построитель: {meta.get('icon','')} {meta.get('label','')}</b>\n"
            f"Поле: <b>{field_label}</b>\n\n"
            f"Шаг 3/4: Введите новое значение для всех ботов:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── Шаг 3: Ввод параметров (текстовые сообщения) ─────────────────────────

@router.message(OpBuilderFSM.entering_params)
async def fsm_ob_entering_params(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите значение:")
        return

    sd = await state.get_data()
    op_type = sd.get("ob_op_type", "")
    meta = _OP_TYPE_META.get(op_type, {})

    if op_type in ("bulk_join", "bulk_leave"):
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            await message.answer("⚠️ Введите хотя бы одну запись:")
            return
        if len(lines) > 50:
            await message.answer("⚠️ Максимум 50 записей за одну операцию.")
            return
        await state.update_data(ob_links=lines)
    else:
        await state.update_data(ob_param=text)

    await state.set_state(OpBuilderFSM.confirming)

    # Собираем preview
    await _ob_show_preview(message, state, pool, meta, op_type, edit=False)


async def _ob_show_preview(msg, state: FSMContext, pool: asyncpg.Pool, meta: dict, op_type: str, edit: bool = False) -> None:
    """Показать preview и кнопку подтверждения."""
    sd = await state.get_data()
    target = sd.get("ob_target", "")
    ob_param = sd.get("ob_param", "")
    ob_links = sd.get("ob_links", [])

    target_label = _TARGET_LABELS.get(target, target) if op_type == "mass_publish" else target

    # Считаем количество аккаунтов/целей
    uid = msg.from_user.id if hasattr(msg, "from_user") else msg.chat.id
    acc_count = 0
    try:
        accounts = await _get_active_accounts(pool, uid)
        acc_count = len(accounts)
    except Exception:
        pass

    lines = []
    lines.append(f"🛠️ <b>Построитель — Предпросмотр операции</b>")
    lines.append("")
    lines.append(f"Тип: {meta.get('icon', '')} <b>{meta.get('label', op_type)}</b>")

    if op_type == "mass_publish":
        lines.append(f"Цели: <b>{target_label}</b>")
        preview_text = html.escape(ob_param[:200])
        lines.append(f"Аккаунтов: <b>{acc_count}</b>")
        lines.append(f"\nТекст поста:\n<i>{preview_text}</i>")
    elif op_type in ("bulk_join", "bulk_leave"):
        action_word = "вступления" if op_type == "bulk_join" else "выхода"
        link_preview = "\n".join(f"• {html.escape(ln)}" for ln in ob_links[:5])
        if len(ob_links) > 5:
            link_preview += f"\n… и ещё {len(ob_links) - 5}"
        lines.append(f"Каналов/групп: <b>{len(ob_links)}</b>")
        lines.append(f"Аккаунтов для {action_word}: <b>{acc_count}</b>")
        lines.append(f"\n<b>Список:</b>\n{link_preview}")
    elif op_type == "bulk_bot_edit":
        _FIELD_LABELS_OB = {
            "name": "Имя", "desc": "Описание",
            "short_desc": "Краткое описание", "commands": "Команды",
        }
        field_label = _FIELD_LABELS_OB.get(target, target)
        preview_val = html.escape(ob_param[:200])
        lines.append(f"Поле: <b>{field_label}</b>")
        lines.append(f"\nЗначение:\n<i>{preview_val}</i>")

    lines.append("")
    lines.append("Шаг 4/4: Подтвердить запуск операции?")

    preview_text_full = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить и поставить в очередь", callback_data=MassOpCb(action="ob_confirm"))
    kb.button(text="❌ Отмена",                            callback_data=MassOpCb(action="menu"))
    kb.adjust(1)

    if edit:
        try:
            await msg.edit_text(preview_text_full, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            pass
    await msg.answer(preview_text_full, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Шаг 4: Подтверждение и запись в operation_queue ──────────────────────

@router.callback_query(MassOpCb.filter(F.action == "ob_confirm"))
async def cb_ob_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю операцию...")
    sd = await state.get_data()
    await state.clear()

    op_type = sd.get("ob_op_type", "")
    target = sd.get("ob_target", "")
    ob_param = sd.get("ob_param", "")
    ob_links = sd.get("ob_links", [])
    uid = callback.from_user.id
    meta = _OP_TYPE_META.get(op_type, {})

    # Формируем params для operation_queue
    if op_type == "mass_publish":
        params = {
            "target": target,
            "filter": "all",
            "text": ob_param,
            "delay": 30,
            "source": "builder",
        }
        total_items = 1  # воркер посчитает реальное кол-во каналов при запуске
    elif op_type == "bulk_join":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        params = {"links": ob_links, "account_ids": acc_ids, "source": "builder"}
        total_items = len(ob_links) * max(1, len(acc_ids))
    elif op_type == "bulk_leave":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        params = {"channels": ob_links, "account_ids": acc_ids, "source": "builder"}
        total_items = len(ob_links) * max(1, len(acc_ids))
    elif op_type == "bulk_bot_edit":
        params = {"field": target, "value": ob_param, "source": "builder"}
        total_items = 1  # воркер посчитает реальное кол-во ботов при запуске
    else:
        await callback.message.edit_text(
            "⚠️ Неизвестный тип операции.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    try:
        op_id = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id, op_type, status, params, total_items)
               VALUES($1, $2, 'pending', $3::jsonb, $4)
               RETURNING id""",
            uid,
            op_type,
            json.dumps(params),
            total_items,
        )
    except Exception as e:
        log.error("ob_confirm insert error: %s", e)
        await callback.message.edit_text(
            "⚠️ Ошибка создания операции. Попробуйте ещё раз.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    icon = meta.get("icon", "")
    label = meta.get("label", op_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Меню",             callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: {icon} <b>{label}</b>\n"
        f"Статус: ⏳ Ожидает выполнения\n\n"
        f"Воркер запустит операцию автоматически.\n"
        f"Следить за прогрессом: <b>Очередь операций</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
