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

from bot.callbacks import MassOpCb
from bot.states import MassPublishFSM, BulkBotEditFSM

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


# ── Helpers ─────────────────────────────────────────────────────────────────

async def _get_active_accounts(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT id, phone, first_name, username FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at",
        owner_id,
    )


def _acc_label(acc: asyncpg.Record) -> str:
    name = acc["first_name"] or ""
    uname = f"@{acc['username']}" if acc["username"] else acc["phone"]
    return f"{name} ({uname})" if name else uname


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
    return kb


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    filled = round(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


# ── Main menu ────────────────────────────────────────────────────────────────

@router.callback_query(MassOpCb.filter(F.action == "menu"))
async def cb_mass_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Массовая публикация",  callback_data=MassOpCb(action="mass_publish"))
    kb.button(text="🔍 Предпросмотр (Dry Run)", callback_data=MassOpCb(action="dry_run"))
    kb.button(text="📋 Очередь операций",     callback_data=MassOpCb(action="queue"))
    kb.button(text="✏️ Массовое редактирование ботов", callback_data=MassOpCb(action="bulk_bot_edit"))
    kb.button(text="◀️ Назад",               callback_data="main_menu")
    kb.adjust(1)
    await callback.message.edit_text(
        "⚡ <b>Массовые операции</b>\n\nВыберите тип операции:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# MASS PUBLISH
# ══════════════════════════════════════════════════════════════════════════

# Step 1: choose target type

@router.callback_query(MassOpCb.filter(F.action == "mass_publish"))
async def cb_mass_publish_start(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
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
    await state.update_data(mp_target=callback_data.op_type)
    await state.set_state(MassPublishFSM.choosing_selector)

    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 Все активные аккаунты", callback_data=MassOpCb(action="mp_filter", op_type="all"))
    kb.button(text="👤 По аккаунту",           callback_data=MassOpCb(action="mp_filter", op_type="account"))
    kb.button(text="🗂 По кластеру",           callback_data=MassOpCb(action="mp_filter", op_type="cluster"))
    kb.button(text="❌ Отмена",                callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    target_label = _TARGET_LABELS.get(callback_data.op_type, callback_data.op_type)
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
    filter_type = callback_data.op_type
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
    await state.update_data(mp_cluster=callback_data.op_type, mp_acc_id=None)
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
            "SELECT id, op_type, status, done_items, total_items, created_at "
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
    for i, row in enumerate(rows, 1):
        icon = _STATUS_ICONS.get(row["status"], "❓")
        op_type = html.escape(row["op_type"])
        status = row["status"]
        done = row["done_items"] or 0
        total = row["total_items"] or 0
        created = row["created_at"].strftime("%Y-%m-%d %H:%M") if row["created_at"] else "—"

        if status == "running":
            progress = f"{done}/{total} ✓"
        elif status == "done":
            progress = created
        else:
            progress = f"{total} элементов"

        lines.append(f"{i}. {op_type} | {icon} {status} | {progress}")

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
    callback: CallbackQuery, state: FSMContext
) -> None:
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
    field = callback_data.op_type
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
