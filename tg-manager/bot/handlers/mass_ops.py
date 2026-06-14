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
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import MassOpCb, BmCb
from services.logger import log_exc_swallow
from services import operation_bus, infra_orchestrator
from bot.utils.event_status import mark_handled_error

try:
    from services import intelligence_engine as _ie
except ImportError:
    _ie: Any | None = None
from bot.states import (
    MassPublishFSM,
    BulkBotEditFSM,
    BulkJoinFSM,
    BulkLeaveFSM,
    OpBuilderFSM,
)
from bot.utils.op_helpers import (
    _acc_label,
    _get_active_accounts,
    _progress_bar,
    safe_edit,
)

from services import task_registry as _treg

log = logging.getLogger(__name__)
router = Router()


# ── Timing options ──────────────────────────────────────────────────────────

_TIMING_OPTIONS = [
    ("⚡ Немедленно", "0"),
    ("⏱ Задержка 5с", "5"),
    ("⏳ Задержка 30с", "30"),
]

_TARGET_LABELS = {
    "channels": "Каналы",
    "groups": "Группы",
    "both": "Каналы и группы",
}

_FILTER_LABELS = {
    "all": "Все активные аккаунты",
    "account": "По аккаунту",
    "cluster": "По кластеру",
    "pool": "По пулу",
}


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
    return kb


_RISK_LABEL = {
    "low": ("🟢", "низкий"),
    "medium": ("🟡", "средний"),
    "high": ("🔴", "высокий"),
}


async def _capacity_line(
    pool: asyncpg.Pool, owner_id: int, op_type: str, total_items: int, acc_ids: list
) -> str:
    """Однострочный прогноз нагрузки для экрана подтверждения. Молча возвращает '' при ошибке."""
    try:
        est = await infra_orchestrator.estimate_capacity(
            pool, owner_id, op_type, total_items, account_ids=acc_ids or None
        )
        minutes = est.get("estimated_minutes", 0)
        risk = "low"
        if minutes > 120:
            risk = "high"
        elif minutes > 45:
            risk = "medium"
        emoji, label = _RISK_LABEL.get(risk, ("⚪", "неизвестно"))
        return f"⏱ Прогноз: ~{minutes:.0f} мин · {emoji} {label} риск"
    except Exception:
        return ""


async def _intel_block(
    pool: asyncpg.Pool, owner_id: int, op_type: str, total_items: int, acc_ids: list
) -> str:
    """Intelligence-блок для экрана подтверждения массовых операций.

    Использует intelligence_engine.get_pre_launch_intelligence() с fallback
    на базовый infra_orchestrator.get_state() + estimate_capacity().
    """
    base_block = ""

    # Primary: full intelligence engine
    if _ie is not None:
        try:
            intel = await _ie.get_pre_launch_intelligence(
                pool, owner_id, op_type, total_items, acc_ids or None
            )
            base_block = _ie.format_pre_launch_block(intel)
        except Exception:
            pass

    if not base_block:
        # Fallback: simple state block via infra_orchestrator
        try:
            state_res, cap_res = await asyncio.gather(
                infra_orchestrator.get_state(pool, owner_id),
                infra_orchestrator.estimate_capacity(
                    pool, owner_id, op_type, total_items, account_ids=acc_ids or None
                ),
            )
            pressure = state_res.pressure_emoji
            p_label = state_res.pressure_label
            p_score = state_res.pressure_score
            available = state_res.account_available
            cooling = state_res.account_cooling
            total_acc = state_res.account_total
            est_min = cap_res.get("estimated_minutes", 0)

            lines = [
                "📊 <b>Анализ операции</b>",
                f"{pressure} Инфраструктура: {p_label} ({p_score}/100)",
                f"👥 Аккаунты: ✅ {available}  ⏳ {cooling}  📱 {total_acc}",
            ]
            if est_min and est_min > 0:
                lines.append(f"⏱ Прогноз выполнения: ~{est_min:.0f} мин")

            recs = state_res.recommendations or []
            shown = 0
            for rec in recs:
                if shown >= 2:
                    break
                if rec.get("severity") in ("critical", "warning"):
                    lines.append(f"⚠️ {rec.get('text', rec.get('message', ''))[:80]}")
                    shown += 1

            base_block = "\n".join(lines)
        except Exception:
            pass

    # Append ecosystem health summary if ecosystems exist
    eco_lines: list[str] = []
    try:
        from services import ecosystem_brain as _eb

        _ecosystems = await _eb.list_ecosystems(pool, owner_id)
        if _ecosystems:
            eco_lines.append("🌐 <b>Экосистемы:</b>")
            for _eco in _ecosystems[:3]:
                _eco_health = await _eb.compute_health(pool, _eco["id"], owner_id)
                _health_pct = int(_eco_health.overall * 100)
                _health_icon = (
                    "🟢"
                    if _eco_health.overall >= 0.7
                    else ("🟡" if _eco_health.overall >= 0.4 else "🔴")
                )
                eco_lines.append(f"  {_health_icon} {_eco['name']}: {_health_pct}%")
    except Exception:
        pass

    parts = [p for p in (base_block, "\n".join(eco_lines)) if p]
    return "\n\n".join(parts)


# ── Main menu ────────────────────────────────────────────────────────────────


@router.callback_query(MassOpCb.filter(F.action == "menu"))
async def cb_mass_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🛠️ Построитель операций", callback_data=MassOpCb(action="build"))
    kb.button(
        text="📤 Массовая публикация", callback_data=MassOpCb(action="mass_publish")
    )
    kb.button(
        text="🔗 Массовый join каналов", callback_data=MassOpCb(action="bulk_join")
    )
    kb.button(
        text="🚪 Массовый выход из каналов", callback_data=MassOpCb(action="bulk_leave")
    )
    kb.button(
        text="✏️ Массовое редактирование ботов",
        callback_data=MassOpCb(action="bulk_bot_edit"),
    )
    kb.button(
        text="🔍 Предпросмотр (Dry Run)", callback_data=MassOpCb(action="dry_run")
    )
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(2, 2, 2, 1, 1)
    await safe_edit(
        callback,
        "🛠️ <b>Построитель операций</b>\n\n"
        "🛠️ <b>Построитель</b> — пошаговый wizard для создания любой операции\n"
        "📤 <b>Публикация</b> — отправить пост во все каналы\n"
        "🔗 <b>Join</b> — вступить в список каналов/групп несколькими аккаунтами\n"
        "🚪 <b>Leave</b> — выйти из каналов/групп несколькими аккаунтами\n"
        "✏️ <b>Редактирование ботов</b> — изменить имя/описание всех ботов сразу\n\n"
        "Выберите тип операции:",
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
        await safe_edit(
            callback,
            "🔒 <b>Массовая публикация — 💎 ПОДПИСКА</b>\n\nОформите подписку: /subscription",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    await state.set_state(MassPublishFSM.choosing_targets)
    await state.update_data(mp_step="targets")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📢 Каналы", callback_data=MassOpCb(action="mp_target", op_type="channels")
    )
    kb.button(
        text="👥 Группы", callback_data=MassOpCb(action="mp_target", op_type="groups")
    )
    kb.button(
        text="📢+👥 Каналы и группы",
        callback_data=MassOpCb(action="mp_target", op_type="both"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 1, 1)
    await safe_edit(
        callback,
        "📤 <b>Массовая публикация</b>\n\nШаг 1 из 5: Выберите тип целей:",
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
    kb.button(
        text="🌐 Все активные аккаунты",
        callback_data=MassOpCb(action="mp_filter", op_type="all"),
    )
    kb.button(
        text="👤 По аккаунту",
        callback_data=MassOpCb(action="mp_filter", op_type="account"),
    )
    kb.button(
        text="🗂 По кластеру",
        callback_data=MassOpCb(action="mp_filter", op_type="cluster"),
    )
    kb.button(
        text="🏊 По пулу", callback_data=MassOpCb(action="mp_filter", op_type="pool")
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    target_label = _TARGET_LABELS.get(_op_type, _op_type)
    await safe_edit(
        callback,
        f"📤 <b>Массовая публикация</b>\n"
        f"Цели: <b>{target_label}</b>\n\n"
        "Шаг 2 из 5: Выберите фильтр аккаунтов:",
        reply_markup=kb.as_markup(),
    )


# Step 2b: filter by account — show account list


@router.callback_query(MassOpCb.filter(F.action == "mp_filter"))
async def cb_mp_filter_chosen(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    filter_type = callback_data.op_type or ""
    await state.update_data(mp_filter=filter_type)
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")

    if filter_type == "account":
        accounts = await _get_active_accounts(pool, callback.from_user.id)
        if not accounts:
            await safe_edit(
                callback,
                "⚠️ Нет активных аккаунтов.",
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
        await safe_edit(
            callback,
            f"📤 <b>Массовая публикация</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 2б: Выберите аккаунт:",
            reply_markup=kb.as_markup(),
        )
        return

    if filter_type == "cluster":
        # Fetch distinct clusters from tg_accounts
        try:
            rows = await pool.fetch(
                "SELECT DISTINCT cluster FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND cluster IS NOT NULL",
                callback.from_user.id,
            )
        except Exception:
            rows = []
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
        await safe_edit(
            callback,
            f"📤 <b>Массовая публикация</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 2б: Выберите кластер:",
            reply_markup=kb.as_markup(),
        )
        return

    if filter_type == "pool":
        from database import db as _db

        pools = await _db.get_distinct_pools(pool, callback.from_user.id)
        if not pools:
            # No pools defined — fall back to "all"
            await state.update_data(
                mp_filter="all", mp_acc_id=None, mp_cluster=None, mp_pool=None
            )
            await _ask_mp_text(callback.message, state, target_label, edit=True)
            return
        kb = InlineKeyboardBuilder()
        for pl in pools:
            kb.button(
                text=f"🏊 {pl}",
                callback_data=MassOpCb(action="mp_pool_pick", op_type=pl[:40]),
            )
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        kb.adjust(1)
        await safe_edit(
            callback,
            f"📤 <b>Массовая публикация</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 2б: Выберите пул аккаунтов:",
            reply_markup=kb.as_markup(),
        )
        return

    # filter_type == "all"
    await state.update_data(mp_acc_id=None, mp_cluster=None, mp_pool=None)
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
    await state.update_data(
        mp_cluster=callback_data.op_type or "", mp_acc_id=None, mp_pool=None
    )
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")
    await _ask_mp_text(callback.message, state, target_label, edit=True)


@router.callback_query(MassOpCb.filter(F.action == "mp_pool_pick"))
async def cb_mp_pool_picked(
    callback: CallbackQuery, callback_data: MassOpCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        mp_pool=callback_data.op_type or "", mp_acc_id=None, mp_cluster=None
    )
    data = await state.get_data()
    target_label = _TARGET_LABELS.get(data.get("mp_target", ""), "")
    await _ask_mp_text(callback.message, state, target_label, edit=True)


# Step 3: enter text


async def _ask_mp_text(
    msg, state: FSMContext, target_label: str, edit: bool = False
) -> None:
    await state.set_state(MassPublishFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    text = (
        f"📤 <b>Массовая публикация</b>\n"
        f"Цели: <b>{target_label}</b>\n\n"
        "Шаг 3 из 5: Введите текст поста (поддерживается HTML)\n"
        "или загрузите <b>.txt файл</b> с текстом:"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось отредактировать сообщение ввода текста массовой публикации, отправляем новое",
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


async def _proceed_mp_text(text: str, message: Message, state: FSMContext) -> None:
    """Common logic after collecting mass publish post text."""
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


@router.message(MassPublishFSM.waiting_text, F.text)
async def fsm_mp_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Введите текст поста:")
        return
    await _proceed_mp_text(text, message, state)


@router.message(MassPublishFSM.waiting_text, F.document)
async def fsm_mp_text_file(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not doc or (doc.mime_type and not doc.mime_type.startswith("text")):
        await message.answer("⚠️ Отправьте текстовый .txt файл с текстом поста.")
        return
    if doc.file_size and doc.file_size > 50_000:
        await message.answer("⚠️ Файл слишком большой. Максимум 50 КБ.")
        return
    try:
        file_info = await message.bot.get_file(doc.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        text = downloaded.read().decode("utf-8", errors="ignore").strip()
    except Exception as e:
        await message.answer(f"⚠️ Не удалось прочитать файл: {e}")
        return
    if not text:
        await message.answer("⚠️ Файл пустой.")
        return
    if len(text) > 4000:
        text = text[:4000]
        await message.answer("⚠️ Текст обрезан до 4000 символов.")
    await _proceed_mp_text(text, message, state)
    return


@router.message(MassPublishFSM.waiting_text)
async def fsm_mp_text_fallback(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    await message.answer(
        "⚠️ Введите текст поста или загрузите .txt файл:", reply_markup=kb.as_markup()
    )


# Step 4: choose timing


@router.callback_query(MassOpCb.filter(F.action == "mp_timing"))
async def cb_mp_timing(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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
    mp_pool = data.get("mp_pool")
    mp_text = data.get("mp_text", "")

    # Count channels for preview
    channel_count = await _count_targets(
        pool,
        callback.from_user.id,
        target,
        filter_type,
        mp_acc_id,
        mp_cluster,
        pool_name=mp_pool,
    )

    target_label = _TARGET_LABELS.get(target, target)
    filter_label = _FILTER_LABELS.get(filter_type, filter_type)
    if filter_type == "account" and mp_acc_id:
        try:
            acc_row = await pool.fetchrow(
                "SELECT first_name, phone FROM tg_accounts WHERE id=$1", mp_acc_id
            )
        except Exception:
            acc_row = None
        if acc_row:
            filter_label = f"Аккаунт: {acc_row['first_name'] or acc_row['phone']}"
    elif filter_type == "cluster" and mp_cluster:
        filter_label = f"Кластер: {mp_cluster}"
    elif filter_type == "pool" and mp_pool:
        filter_label = f"Пул: {mp_pool}"

    delay_label = f"{delay}с" if delay > 0 else "Немедленно"
    estimated_mins = round(channel_count * delay / 60, 1) if delay else 0
    preview_text = html.escape(mp_text[:300])

    await state.set_state(MassPublishFSM.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=MassOpCb(action="mp_confirm"))
    kb.button(text="🔍 Dry Run", callback_data=MassOpCb(action="dry_run"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 1)
    await safe_edit(
        callback,
        f"📤 <b>Предпросмотр публикации</b>\n\n"
        f"Тип целей: <b>{target_label}</b>\n"
        f"Фильтр: <b>{filter_label}</b>\n"
        f"Ожидается: <b>~{channel_count}</b> каналов/групп\n"
        f"Задержка: <b>{delay_label}</b>\n"
        f"Расчётное время: ~{estimated_mins} мин\n\n"
        f"Текст:\n<i>{preview_text}</i>\n\n"
        "Подтвердить запуск?",
        reply_markup=kb.as_markup(),
    )


# Step 5: confirm and run


@router.callback_query(MassOpCb.filter(F.action == "mp_confirm"))
async def cb_mp_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    # Проверка давления инфраструктуры
    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    await callback.answer(warn or "⏳ Ставлю в очередь...", show_alert=bool(warn))

    data = await state.get_data()
    await state.clear()

    target = data.get("mp_target", "channels")
    filter_type = data.get("mp_filter", "all")
    mp_acc_id = data.get("mp_acc_id")
    mp_cluster = data.get("mp_cluster")
    mp_pool = data.get("mp_pool")
    mp_text = data.get("mp_text", "")
    delay = int(data.get("mp_delay", 0))

    # Validate accounts exist for the chosen filter
    accounts = await _get_accounts_for_filter(
        pool,
        callback.from_user.id,
        filter_type,
        mp_acc_id,
        mp_cluster,
        pool_name=mp_pool,
    )

    if not accounts:
        filter_hint = {
            "account": "выбранного аккаунта",
            "cluster": f"кластера «{mp_cluster}»",
            "pool": f"пула «{mp_pool}»",
        }.get(filter_type, "выбранного фильтра")
        await safe_edit(
            callback,
            f"⚠️ Нет активных аккаунтов для {filter_hint}.\n"
            "Проверьте аккаунты в разделе 📱 Аккаунты.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    # Count expected targets to give useful preview (op_worker will do actual fetch)
    channel_count = await _count_targets(
        pool,
        callback.from_user.id,
        target,
        filter_type,
        mp_acc_id,
        mp_cluster,
        pool_name=mp_pool,
    )

    if channel_count == 0:
        # Check if user has any managed channels at all
        try:
            has_managed = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
                    callback.from_user.id,
                )
                or 0
            )
        except Exception:
            has_managed = 0
        from bot.callbacks import ChanCb as _ChanCb

        empty_kb = InlineKeyboardBuilder()
        if has_managed == 0:
            empty_kb.button(
                text="📡 Перейти в раздел Каналы", callback_data=_ChanCb(action="menu")
            )
        empty_kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
        empty_kb.adjust(1)
        hint = (
            "⚠️ <b>Нет каналов для рассылки</b>\n\n"
            "Ни один из ваших аккаунтов не состоит в каналах/группах выбранного типа.\n\n"
            "💡 Сначала создайте или импортируйте канал в разделе <b>📡 Каналы &amp; операции</b>."
            if has_managed == 0
            else "⚠️ Нет подходящих каналов/групп для рассылки.\n\n"
            "Попробуйте изменить фильтр или тип целей."
        )
        await safe_edit(callback, hint, reply_markup=empty_kb.as_markup())
        return

    # Build account_ids param for op_worker (filter-aware)
    acc_id_list = [acc["id"] for acc in accounts]

    # Submit to operation_bus — op_worker handles actual execution with proper
    # account claiming, flood handling, progress tracking and retry logic.
    # Do NOT use _create_op_record (status='running') + inline bg task here:
    # that creates a duplicate execution path and causes double-execution on restart.
    params = {
        "target": target,
        "filter": filter_type,
        "delay_seconds": delay,
        "delay": delay,
        "text": mp_text,
        "mp_text": mp_text,
        "account_ids": acc_id_list if filter_type != "all" else [],
    }
    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "mass_publish",
            params,
            total_items=channel_count,
        )
    except Exception as e:
        log.error("mp_confirm submit error: %s", e)
        await safe_edit(
            callback,
            "⚠️ Ошибка постановки операции в очередь. Попробуйте ещё раз.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    target_label = _TARGET_LABELS.get(target, target)
    delay_label = f"{delay}с" if delay > 0 else "немедленно"
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Меню", callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    await safe_edit(
        callback,
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: 📤 Массовая публикация\n"
        f"Цели: <b>{target_label}</b>\n"
        f"Каналов: <b>~{channel_count}</b>\n"
        f"Задержка: <b>{delay_label}</b>\n\n"
        f"Воркер запустит операцию автоматически.\n"
        f"Следить за прогрессом: <b>Очередь операций</b>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "cancel_op"))
async def cb_cancel_op(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    # Cancel both pending and running operations
    try:
        result = await pool.execute(
            "UPDATE operation_queue SET status='cancelled', finished_at=now() "
            "WHERE id=$1 AND owner_id=$2 AND status IN ('pending','running')",
            callback_data.op_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"cancel_op: {exc}")
        await callback.answer(f"Ошибка БД: {exc}", show_alert=True)
        return
    if result == "UPDATE 0":
        await callback.answer("Операция уже завершена или не найдена.", show_alert=True)
        return
    await callback.answer()
    # Refresh queue view — re-use cb_queue to avoid duplicate rendering logic
    await cb_queue(callback, callback_data, pool)


@router.callback_query(MassOpCb.filter(F.action == "retry_op"))
async def cb_retry_op(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    try:
        result = await pool.execute(
            "UPDATE operation_queue SET status='pending', last_error=NULL, error_msg=NULL, "
            "retry_count=0, started_at=NULL, finished_at=NULL, done_items=0, "
            "scheduled_for=NULL "
            "WHERE id=$1 AND owner_id=$2 AND status='failed'",
            callback_data.op_id,
            callback.from_user.id,
        )
    except Exception as e:
        await callback.answer(f"Ошибка БД: {e}", show_alert=True)
        return
    if result == "UPDATE 0":
        await callback.answer("Операция не найдена или уже выполнена.", show_alert=True)
        return
    await callback.answer(f"✅ Операция #{callback_data.op_id} поставлена в очередь повторно.", show_alert=True)
    # Re-render queue view — re-use cb_queue to avoid duplicate rendering logic
    await cb_queue(callback, callback_data, pool)


@router.callback_query(MassOpCb.filter(F.action == "retry_failed"))
async def cb_retry_all_failed(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    """Сбросить ВСЕ неудачные операции пользователя в статус pending для повторного выполнения."""
    try:
        result = await pool.execute(
            "UPDATE operation_queue SET status='pending', last_error=NULL, error_msg=NULL, "
            "retry_count=0, started_at=NULL, finished_at=NULL, done_items=0, "
            "scheduled_for=NULL "
            "WHERE owner_id=$1 AND status='failed'",
            callback.from_user.id,
        )
    except Exception as e:
        await callback.answer(f"Ошибка БД: {e}", show_alert=True)
        return
    try:
        reset_count = int(str(result).split()[-1])
    except (ValueError, IndexError):
        reset_count = 0
    if reset_count == 0:
        await callback.answer("Нет неудачных операций для повторного запуска.", show_alert=True)
        return
    await callback.answer(
        f"✅ {reset_count} операц{'ия' if reset_count == 1 else 'ии' if 2 <= reset_count <= 4 else 'ий'} "
        f"поставлено в очередь повторно.",
        show_alert=True,
    )
    # Re-render queue view
    await cb_queue(callback, MassOpCb(action="queue", op_type="all", page=0), pool)


@router.callback_query(MassOpCb.filter(F.action == "op_detail"))
async def cb_op_detail(
    callback: CallbackQuery, callback_data: MassOpCb, pool: asyncpg.Pool
) -> None:
    """Показать детальный лог шагов операции из таблицы operation_log."""
    await callback.answer()
    op_id = callback_data.op_id
    user_id = callback.from_user.id

    try:
        op = await pool.fetchrow(
            "SELECT id, op_type, status, done_items, total_items, created_at, "
            "last_error, retry_count, max_retries, finished_at, result "
            "FROM operation_queue WHERE id=$1 AND owner_id=$2",
            op_id, user_id,
        )
    except Exception as e:
        await callback.answer(f"Ошибка БД: {e}", show_alert=True)
        return

    if not op:
        await callback.answer("Операция не найдена.", show_alert=True)
        return

    try:
        log_rows = await pool.fetch(
            "SELECT step_num, target, status, message, created_at "
            "FROM operation_log WHERE op_id=$1 ORDER BY step_num DESC LIMIT 30",
            op_id,
        )
    except Exception:
        log_rows = []

    _STATUS_ICONS = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌", "cancelled": "🚫"}
    _LOG_ICONS = {"ok": "✅", "skip": "⏭", "error": "❌"}
    icon = _STATUS_ICONS.get(op["status"], "❓")
    op_type_label = html.escape(op["op_type"])
    done = op["done_items"] or 0
    total = op["total_items"] or 0
    created = op["created_at"].strftime("%d.%m.%Y %H:%M") if op["created_at"] else "—"
    finished = op["finished_at"].strftime("%d.%m %H:%M") if op["finished_at"] else "—"

    lines = [
        f"🔍 <b>Детали операции #{op_id}</b>",
        f"{icon} <b>{op_type_label}</b>  [{op['status']}]",
        f"Прогресс: {done}/{total}  ·  Создана: {created}",
    ]
    if op["finished_at"]:
        lines.append(f"Завершена: {finished}")
    if op["last_error"]:
        lines.append(f"⚠️ Последняя ошибка: <i>{html.escape(op['last_error'][:150])}</i>")

    # Result summary if done
    if op["result"]:
        try:
            res_data = op["result"] if isinstance(op["result"], dict) else json.loads(op["result"])
            summary = res_data.get("summary", "")
            if summary:
                lines.append(f"\n📊 <b>Итог:</b> {html.escape(summary[:200])}")
        except Exception:
            pass

    # Per-step log
    if log_rows:
        lines.append(f"\n📋 <b>Последние шаги</b> (из {len(log_rows)}):")
        for row in log_rows:
            step_icon = _LOG_ICONS.get(row["status"], "❓")
            target_str = html.escape(str(row["target"] or "")[:40])
            msg_str = html.escape(str(row["message"] or "")[:60])
            step_time = row["created_at"].strftime("%H:%M:%S") if row["created_at"] else ""
            step_line = f"  {step_icon} #{row['step_num']} {target_str}"
            if msg_str:
                step_line += f" — {msg_str}"
            if step_time:
                step_line += f" <i>{step_time}</i>"
            lines.append(step_line)
    else:
        lines.append("\n<i>Детальный лог шагов недоступен.</i>")

    kb = InlineKeyboardBuilder()
    if op["status"] == "failed":
        kb.button(
            text="🔄 Повторить",
            callback_data=MassOpCb(action="retry_op", op_id=op_id),
        )
    kb.button(
        text="◀️ В очередь",
        callback_data=MassOpCb(action="queue", op_type="all", page=0),
    )
    kb.adjust(1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


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
    mp_pool = data.get("mp_pool")
    delay = int(data.get("mp_delay", 30))
    mp_text = data.get("mp_text", "")

    target_label = _TARGET_LABELS.get(target, "Каналы")
    filter_label = _FILTER_LABELS.get(filter_type, "Все активные аккаунты")

    channel_count = await _count_targets(
        pool,
        callback.from_user.id,
        target,
        filter_type,
        mp_acc_id,
        mp_cluster,
        pool_name=mp_pool,
    )
    estimated_mins = round(channel_count * delay / 60, 1) if delay else 0
    delay_label = f"{delay}с" if delay > 0 else "Немедленно"

    kb = InlineKeyboardBuilder()
    if mp_text:
        kb.button(text="✅ Запустить", callback_data=MassOpCb(action="mp_confirm"))
    else:
        kb.button(
            text="📤 Настроить публикацию",
            callback_data=MassOpCb(action="mass_publish"),
        )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)

    await safe_edit(
        callback,
        f"🔍 <b>Предпросмотр операции</b>\n\n"
        f"Тип: Публикация в {target_label.lower()}\n"
        f"Фильтр: {filter_label}\n"
        f"Каналов ожидается: <b>~{channel_count}</b>\n"
        f"Расчётное время: ~{estimated_mins} мин\n"
        f"Задержка между постами: {delay_label}\n\n"
        f"<i>Операция ещё не выполнена — это только предпросмотр.</i>",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# OPERATION QUEUE
# ══════════════════════════════════════════════════════════════════════════


_QUEUE_PAGE_SIZE = 8
_QUEUE_STATUS_FILTERS = {
    "all": ("Все", None),
    "active": ("Активные", ["pending", "running"]),
    "done": ("Завершённые", ["done"]),
    "failed": ("Ошибки", ["failed"]),
}


@router.callback_query(MassOpCb.filter(F.action == "queue"))
async def cb_queue(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    page = callback_data.page
    # op_type field reused as status filter key (e.g. "all", "active", "failed")
    status_filter_key = callback_data.op_type or "all"
    if status_filter_key not in _QUEUE_STATUS_FILTERS:
        status_filter_key = "all"
    _, allowed_statuses = _QUEUE_STATUS_FILTERS[status_filter_key]

    user_id = callback.from_user.id
    limit = _QUEUE_PAGE_SIZE
    offset = page * limit

    try:
        if allowed_statuses:
            rows = await pool.fetch(
                "SELECT id, op_type, status, done_items, total_items, created_at, "
                "last_error, retry_count, max_retries, finished_at, result "
                "FROM operation_queue "
                "WHERE owner_id=$1 AND status = ANY($4::text[]) "
                "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                user_id, limit, offset, allowed_statuses,
            )
            total_count = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status = ANY($2::text[])",
                user_id, allowed_statuses,
            ) or 0
        else:
            rows = await pool.fetch(
                "SELECT id, op_type, status, done_items, total_items, created_at, "
                "last_error, retry_count, max_retries, finished_at, result "
                "FROM operation_queue "
                "WHERE owner_id=$1 "
                "ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                user_id, limit, offset,
            )
            total_count = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1", user_id
            ) or 0
    except asyncpg.exceptions.UndefinedTableError:
        rows = []
        total_count = 0
    except Exception as e:
        log.warning("Queue fetch error: %s", e)
        rows = []
        total_count = 0

    total_pages = max(1, -(-total_count // limit))

    kb = InlineKeyboardBuilder()

    # Status filter buttons
    for fkey, (flabel, _) in _QUEUE_STATUS_FILTERS.items():
        marker = "▸ " if fkey == status_filter_key else ""
        kb.button(
            text=f"{marker}{flabel}",
            callback_data=MassOpCb(action="queue", op_type=fkey, page=0),
        )
    kb.adjust(len(_QUEUE_STATUS_FILTERS))

    if not rows:
        kb.button(text="🔄 Обновить", callback_data=MassOpCb(action="queue", op_type=status_filter_key, page=0))
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
        kb.adjust(len(_QUEUE_STATUS_FILTERS), 2)
        await safe_edit(
            callback,
            "📋 <b>Очередь операций</b>\n\nОперации не найдены.\n\n"
            "💡 Запустите операцию через меню Масс-Опс или Каналы",
            reply_markup=kb.as_markup(),
        )
        return

    _STATUS_ICONS = {
        "pending": "⏳",
        "running": "🔄",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }
    filter_label, _ = _QUEUE_STATUS_FILTERS[status_filter_key]
    lines = [f"📋 <b>Очередь операций</b>  [{filter_label}]  стр. {page + 1}/{total_pages}\n"]
    has_completed = False
    failed_count = 0
    for i, r in enumerate(rows, offset + 1):
        icon = _STATUS_ICONS.get(r["status"], "❓")
        op_type_label = html.escape(r["op_type"])
        status = r["status"]
        done = r["done_items"] or 0
        total = r["total_items"] or 0
        created = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "—"
        retry_count = r["retry_count"] or 0
        max_retries = r["max_retries"] or 0
        last_error = r["last_error"] or ""

        # Determine if this is a permanently failed (dead letter) operation
        is_dead_letter = (
            status == "failed"
            and max_retries > 0
            and retry_count >= max_retries
        )

        if status == "running":
            bar = _progress_bar(done, total)
            pct = round(100 * done / total) if total else 0
            progress = f"{done}/{total} [{bar}] {pct}%"
        elif status == "done":
            # Show real result summary from op_worker, not just the date
            result_summary = ""
            try:
                if r["result"]:
                    res_data = (
                        r["result"]
                        if isinstance(r["result"], dict)
                        else json.loads(r["result"])
                    )
                    result_summary = res_data.get("summary", "")
            except Exception:
                pass
            progress = html.escape(result_summary[:70]) if result_summary else f"✓ {done}/{total} · {created}"
        elif status == "failed":
            if is_dead_letter:
                progress = f"🪦 Все {retry_count}/{max_retries} попыток исчерпаны · {created}"
            else:
                progress = f"попытка {retry_count}/{max_retries} · {created}"
        else:
            progress = f"{total} элементов · {created}"

        if is_dead_letter:
            lines.append(f"{i}. ☠️ <b>{op_type_label}</b> #{r['id']} <i>(постоянная ошибка)</i>")
        else:
            lines.append(f"{i}. {icon} <b>{op_type_label}</b> #{r['id']}")
        lines.append(f"   {progress}")

        if status == "failed" and last_error:
            # For dead letter ops, show full actionable error; otherwise truncate
            err_len = 120 if is_dead_letter else 70
            err_preview = html.escape(last_error[:err_len])
            if is_dead_letter:
                lines.append(f"   ❗ <b>Причина:</b> <i>{err_preview}</i>")
                lines.append(f"   💡 <i>Нажмите «Повторить» после устранения проблемы.</i>")
            else:
                lines.append(f"   ⚠️ <i>{err_preview}</i>")

        if status in ("pending", "running"):
            kb.button(
                text=f"❌ Отменить #{r['id']}",
                callback_data=MassOpCb(action="cancel_op", op_id=r["id"]),
            )
        elif status == "failed":
            btn_label = f"🔄 Перезапустить #{r['id']}" if is_dead_letter else f"🔄 Повторить #{r['id']}"
            kb.button(
                text=btn_label,
                callback_data=MassOpCb(action="retry_op", op_id=r["id"]),
            )
            failed_count += 1

        if status in ("done", "failed"):
            has_completed = True
            kb.button(
                text=f"🔍 Детали #{r['id']}",
                callback_data=MassOpCb(action="op_detail", op_id=r["id"]),
            )

    # Pagination navigation
    nav_btns = 0
    if page > 0:
        kb.button(
            text="◀️",
            callback_data=MassOpCb(action="queue", op_type=status_filter_key, page=page - 1),
        )
        nav_btns += 1
    if (page + 1) * limit < total_count:
        kb.button(
            text="▶️",
            callback_data=MassOpCb(action="queue", op_type=status_filter_key, page=page + 1),
        )
        nav_btns += 1

    if failed_count > 1:
        kb.button(
            text=f"🔄 Повторить все ошибки ({failed_count})",
            callback_data=MassOpCb(action="retry_failed"),
        )
    if has_completed:
        kb.button(
            text="🗑 Очистить завершённые",
            callback_data=MassOpCb(action="clear_completed"),
        )
    kb.button(text="🔄 Обновить", callback_data=MassOpCb(action="queue", op_type=status_filter_key, page=page))
    kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))

    # Build adjust list: filter row + action buttons per item + nav + util
    # Each done/failed op has 2 buttons (retry/cancel + detail), pending/running has 1 (cancel)
    action_btn_count = sum(
        2 if r["status"] in ("done", "failed") else 1
        for r in rows
        if r["status"] in ("pending", "running", "done", "failed")
    )
    adjustments = [len(_QUEUE_STATUS_FILTERS)]
    adjustments += [2 if r["status"] in ("done", "failed") else 1
                    for r in rows if r["status"] in ("pending", "running", "done", "failed")]
    if nav_btns:
        adjustments.append(nav_btns)
    if failed_count > 1:
        adjustments.append(1)
    if has_completed:
        adjustments.append(1)
    adjustments.append(2)
    kb.adjust(*adjustments)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(MassOpCb.filter(F.action == "clear_completed"))
async def cb_clear_completed(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Удалить записи со статусом done/failed старше 24 часов, затем показать очередь."""
    try:
        result = await pool.execute(
            "DELETE FROM operation_queue "
            "WHERE owner_id=$1 AND status IN ('done', 'failed') "
            "AND finished_at < now() - interval '24 hours'",
            callback.from_user.id,
        )
        try:
            deleted = int(str(result).split()[-1])
        except (ValueError, IndexError):
            deleted = 0
    except Exception as e:
        log.warning("clear_completed error: %s", e)
        await callback.answer("Ошибка при очистке.", show_alert=True)
        return

    if deleted == 0:
        await callback.answer(
            "Нет завершённых операций старше 24 ч для удаления.", show_alert=True
        )
    else:
        await callback.answer(f"Удалено {deleted} записей.", show_alert=True)

    # Re-use cb_queue to render the updated queue (avoids duplicate rendering logic)
    await cb_queue(callback, MassOpCb(action="queue", op_type="all", page=0), pool)


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
        await safe_edit(
            callback,
            "🔒 <b>Массовое редактирование ботов — 💎 ПОДПИСКА</b>\n\nОформите подписку: /subscription",
            reply_markup=subscription_locked_markup(
                "pro", back_callback=BmCb(action="operations")
            ),
        )
        return
    await callback.answer()
    await state.set_state(BulkBotEditFSM.choosing_field)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Имя бота", callback_data=MassOpCb(action="bbe_field", op_type="name")
    )
    kb.button(
        text="📄 Описание", callback_data=MassOpCb(action="bbe_field", op_type="desc")
    )
    kb.button(
        text="📝 Краткое описание",
        callback_data=MassOpCb(action="bbe_field", op_type="short_desc"),
    )
    kb.button(
        text="⌨️ Команды", callback_data=MassOpCb(action="bbe_field", op_type="commands")
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await safe_edit(
        callback,
        "✏️ <b>Массовое редактирование ботов</b>\n\nВыберите поле для изменения:",
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
        "name": "имя бота",
        "desc": "описание",
        "short_desc": "краткое описание",
        "commands": "команды (формат: /cmd - описание)",
    }
    field_label = _FIELD_LABELS.get(field, field)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="bulk_bot_edit"))
    await safe_edit(
        callback,
        f"✏️ <b>Массовое редактирование</b>\n\nВведите новое <b>{field_label}</b> для всех ботов:",
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
    kb.button(
        text="✅ Применить ко всем ботам", callback_data=MassOpCb(action="bbe_confirm")
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
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

    # Fetch all active bots for this user (same filter as op_worker uses)
    try:
        bots = await pool.fetch(
            "SELECT id, token FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"bbe_confirm bots: {exc}")
        await safe_edit(
            callback,
            f"❌ Ошибка загрузки ботов: <code>{html.escape(str(exc)[:200])}</code>",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    if not bots:
        await safe_edit(
            callback,
            "⚠️ У вас нет добавленных ботов.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "bulk_bot_edit",
            {"field": field, "value": value},
            total_items=len(bots),
        )
    except Exception as e:
        log.error("bbe_confirm submit error: %s", e)
        await safe_edit(
            callback,
            "⚠️ Ошибка постановки в очередь.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    if not op_id:
        await safe_edit(
            callback,
            "⚠️ Не удалось создать операцию. Попробуйте ещё раз.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    _FIELD_LABEL = {
        "name": "Имя",
        "desc": "Описание",
        "short_desc": "Краткое описание",
        "commands": "Команды",
    }
    await safe_edit(
        callback,
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: ✏️ Массовое редактирование ботов\n"
        f"Поле: <b>{_FIELD_LABEL.get(field, field)}</b>\n"
        f"Ботов: <b>{len(bots)}</b>\n\n"
        f"Воркер применит изменения автоматически.",
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
    pool: asyncpg.Pool,
    owner_id: int,
    filter_type: str,
    acc_id: int | None,
    cluster: str | None,
    pool_name: str | None = None,
) -> list[asyncpg.Record]:
    _cols = (
        "id, session_str, first_name, phone, device_model, system_version, app_version, "
        "trust_score, cooldown_until"
    )
    try:
        if filter_type == "account" and acc_id:
            return await pool.fetch(
                f"SELECT {_cols} FROM tg_accounts "
                "WHERE id=$1 AND owner_id=$2 AND is_active=TRUE "
                "AND (cooldown_until IS NULL OR cooldown_until < now())",
                acc_id,
                owner_id,
            )
        if filter_type == "cluster" and cluster:
            return await pool.fetch(
                f"SELECT {_cols} FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND cluster=$2 "
                "AND (cooldown_until IS NULL OR cooldown_until < now()) "
                "ORDER BY trust_score DESC NULLS LAST",
                owner_id,
                cluster,
            )
        if filter_type == "pool" and pool_name:
            return await pool.fetch(
                f"SELECT {_cols} FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND pool=$2 "
                "AND (cooldown_until IS NULL OR cooldown_until < now()) "
                "ORDER BY trust_score DESC NULLS LAST",
                owner_id,
                pool_name,
            )
        return await pool.fetch(
            f"SELECT {_cols} FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE "
            "AND (cooldown_until IS NULL OR cooldown_until < now()) "
            "ORDER BY trust_score DESC NULLS LAST",
            owner_id,
        )
    except Exception as e:
        log.warning("_get_accounts_for_filter error: %s", e)
        return []


async def _count_targets(
    pool: asyncpg.Pool,
    owner_id: int,
    target: str,
    filter_type: str,
    acc_id: int | None,
    cluster: str | None,
    pool_name: str | None = None,
) -> int:
    """Estimate number of matching dialogs. Real count requires fetching Telethon dialogs (slow).

    Uses managed_channels as a DB-backed estimate. Returns 0 if no matching accounts.
    """
    accounts = await _get_accounts_for_filter(
        pool, owner_id, filter_type, acc_id, cluster, pool_name=pool_name
    )
    if not accounts:
        return 0
    acc_ids = [a["id"] for a in accounts]
    # Use managed_channels count filtered by matching accounts where possible
    try:
        if target == "channels":
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id = ANY($2::bigint[])",
                    owner_id,
                    acc_ids,
                )
                or 0
            )
            if db_count > 0:
                return db_count
            # Fallback: all channels for owner regardless of account filter
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
                    owner_id,
                )
                or 0
            )
            if db_count > 0:
                return db_count
        elif target == "groups":
            # managed_channels stores both channels and groups — use same table
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id = ANY($2::bigint[])",
                    owner_id,
                    acc_ids,
                )
                or 0
            )
            if db_count > 0:
                return db_count
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
                    owner_id,
                )
                or 0
            )
            if db_count > 0:
                return db_count
        elif target == "both":
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id = ANY($2::bigint[])",
                    owner_id,
                    acc_ids,
                )
                or 0
            )
            if db_count > 0:
                return db_count
            db_count = (
                await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1",
                    owner_id,
                )
                or 0
            )
            if db_count > 0:
                return db_count
    except Exception:
        pass
    # Last resort: estimate based on account count
    return max(len(accounts), 1) * 3



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
        await safe_edit(
            callback,
            "🔒 <b>Массовый join — 💎 ПОДПИСКА</b>\n\nОформите подписку: /subscription",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await state.set_state(BulkJoinFSM.waiting_links)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    await safe_edit(
        callback,
        "🔗 <b>Массовый join — Шаг 1/4</b>\n\n"
        "Введите ссылки или юзернеймы каналов/групп — <b>по одному на строку</b>:\n\n"
        "<code>@channel_name\n"
        "https://t.me/channel_name\n"
        "https://t.me/+InviteHash</code>\n\n"
        "Или <b>загрузите .txt файл</b> со списком (макс. 50 строк).",
        reply_markup=kb.as_markup(),
    )


async def _process_bj_links(
    links: list[str], message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Common logic after collecting bulk_join links (text or file)."""
    await state.update_data(bj_links=links)
    await state.set_state(BulkJoinFSM.choosing_accounts)
    accounts = await _get_active_accounts(pool, message.from_user.id)
    if not accounts:
        await state.clear()
        from bot.callbacks import AccCb as _AccCb

        kb = InlineKeyboardBuilder()
        kb.button(text="📱 Перейти к аккаунтам", callback_data=_AccCb(action="menu"))
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
        kb.adjust(1)
        await message.answer(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для массового join нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "💡 Добавьте аккаунт в разделе <b>📱 Аккаунты</b>, затем повторите операцию.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    kb.button(
        text="👥 Все активные аккаунты",
        callback_data=MassOpCb(action="bj_accs", op_type="all"),
    )
    for acc in accounts[:10]:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=MassOpCb(action="bj_accs", op_id=acc["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🔗 <b>Массовый join — Шаг 2/4</b>\n\n"
        f"Каналов/групп: <b>{len(links)}</b>\n\n"
        "Выберите аккаунты для вступления:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkJoinFSM.waiting_links, F.text)
async def fsm_bulk_join_links(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    import re as _re

    _link_re = _re.compile(r"^(@[\w]{4,}\s*$|https?://t\.me/[\w\-+/]+|[-\d]{8,})")
    raw = message.text or ""
    links = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not links:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        await message.answer(
            "⚠️ Введите хотя бы одну ссылку или юзернейм:", reply_markup=kb.as_markup()
        )
        return
    if len(links) > 50:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        await message.answer(
            "⚠️ Максимум 50 ссылок за одну операцию.", reply_markup=kb.as_markup()
        )
        return
    bad = [ln for ln in links if not _link_re.match(ln)]
    if bad:
        sample = "\n".join(f"  • <code>{ln[:50]}</code>" for ln in bad[:3])
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        await message.answer(
            f"⚠️ Неверный формат ссылок ({len(bad)} шт.):\n{sample}\n\n"
            "Ожидается: @username, https://t.me/... или ID (−1001234567890)",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    await _process_bj_links(links, message, state, pool)


@router.message(BulkJoinFSM.waiting_links, F.document)
async def fsm_bulk_join_links_file(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    doc = message.document
    if not doc or (doc.mime_type and not doc.mime_type.startswith("text")):
        await message.answer("⚠️ Отправьте текстовый файл (.txt) со списком ссылок.")
        return
    if doc.file_size and doc.file_size > 100_000:
        await message.answer("⚠️ Файл слишком большой. Максимум 100 КБ.")
        return
    try:
        file_info = await message.bot.get_file(doc.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        raw = downloaded.read().decode("utf-8", errors="ignore")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось прочитать файл: {e}")
        return
    links = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not links:
        await message.answer("⚠️ Файл пустой или не содержит ссылок.")
        return
    if len(links) > 200:
        links = links[:200]
        await message.answer("⚠️ Взяты первые 200 строк из файла.")
    await _process_bj_links(links, message, state, pool)


@router.callback_query(MassOpCb.filter(F.action == "bj_accs"))
async def cb_bulk_join_accs(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    if not links:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    uid = callback.from_user.id
    acc_list_preview = ""
    if callback_data.op_type == "all":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        acc_names = [_acc_label(a) for a in accounts[:5]]
        acc_label = f"все ({len(acc_ids)})"
        acc_list_preview = "\n".join(f"  👤 {html.escape(n)}" for n in acc_names)
        if len(acc_ids) > 5:
            acc_list_preview += f"\n  … и ещё {len(acc_ids) - 5}"
    else:
        acc_ids = [callback_data.op_id]
        try:
            acc = await pool.fetchrow(
                "SELECT phone, first_name FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                callback_data.op_id,
                uid,
            )
        except Exception:
            acc = None
        acc_label = acc["phone"] if acc else f"id{callback_data.op_id}"
        acc_list_preview = f"  👤 {html.escape(acc_label)}"

    if not acc_ids:
        await callback.answer("Нет активных аккаунтов", show_alert=True)
        return
    await callback.answer()

    # Show preview + delay selector
    link_preview = "\n".join(f"• {html.escape(ln)}" for ln in links[:5])
    if len(links) > 5:
        link_preview += f"\n… и ещё {len(links) - 5}"

    await state.update_data(bj_acc_ids=acc_ids, bj_acc_label=acc_label)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚡ Быстро (45-90с)",
        callback_data=MassOpCb(action="bj_delay", op_type="fast"),
    )
    kb.button(
        text="🛡 Нормально (30-60с)",
        callback_data=MassOpCb(action="bj_delay", op_type="normal"),
    )
    kb.button(
        text="🐌 Медленно (60-120с)",
        callback_data=MassOpCb(action="bj_delay", op_type="slow"),
    )
    kb.button(
        text="🧠 Умный (авто)",
        callback_data=MassOpCb(action="bj_delay", op_type="smart"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    acc_section = f"\n<b>Аккаунты:</b>\n{acc_list_preview}" if acc_list_preview else ""
    await safe_edit(
        callback,
        f"🔗 <b>Массовый join — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>{acc_section}\n"
        f"Каналов/групп: <b>{len(links)}</b>\n\n"
        f"<b>Список каналов:</b>\n{link_preview}\n\n"
        f"Выберите режим задержки между вступлениями:",
        reply_markup=kb.as_markup(),
    )


_DELAY_LABELS = {
    "fast": ("⚡ Быстро", "45–90с", "7–25 мин"),
    "normal": ("🛡 Нормально", "30–60с", "5–15 мин"),
    "slow": ("🐌 Медленно", "60–120с", "10–30 мин"),
    "smart": ("🧠 Умный", "авто", "переменно"),
}


@router.callback_query(MassOpCb.filter(F.action == "bj_redelay"))
async def cb_bulk_join_redelay(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к выбору задержки в bulk_join (сохраняя выбранные аккаунты)."""
    await callback.answer()
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    acc_label = sd.get("bj_acc_label", "?")

    link_preview = "\n".join(f"• {html.escape(ln)}" for ln in links[:5])
    if len(links) > 5:
        link_preview += f"\n… и ещё {len(links) - 5}"

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚡ Быстро (45-90с)",
        callback_data=MassOpCb(action="bj_delay", op_type="fast"),
    )
    kb.button(
        text="🛡 Нормально (30-60с)",
        callback_data=MassOpCb(action="bj_delay", op_type="normal"),
    )
    kb.button(
        text="🐌 Медленно (60-120с)",
        callback_data=MassOpCb(action="bj_delay", op_type="slow"),
    )
    kb.button(
        text="🧠 Умный (авто)",
        callback_data=MassOpCb(action="bj_delay", op_type="smart"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await safe_edit(
        callback,
        f"🔗 <b>Массовый join — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(links)}</b>\n\n"
        f"<b>Список:</b>\n{link_preview}\n\n"
        f"Выберите режим задержки между вступлениями:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bj_delay"))
async def cb_bulk_join_delay(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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

    icon, delay_str, time_est = _DELAY_LABELS.get(
        delay_mode, ("🧠 Умный", "авто", "переменно")
    )
    n = len(links) * len(acc_ids)
    cap_line, intel = await asyncio.gather(
        _capacity_line(pool, callback.from_user.id, "join", n, acc_ids),
        _intel_block(pool, callback.from_user.id, "join", n, acc_ids),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить join", callback_data=MassOpCb(action="bj_confirm"))
    kb.button(text="◀️ Изменить задержку", callback_data=MassOpCb(action="bj_redelay"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await safe_edit(
        callback,
        f"🔗 <b>Массовый join — Шаг 4/4 (Подтверждение)</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(links)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n"
        f"Операций: <b>{n}</b> (~{time_est})\n"
        + (f"{cap_line}\n" if cap_line else "")
        + (f"\n{intel}\n" if intel else "")
        + f"\n<b>Список:</b>\n{link_preview}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bj_confirm"))
async def cb_bulk_join_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    links = sd.get("bj_links", [])
    acc_ids = sd.get("bj_acc_ids", [])
    delay_mode = sd.get("bj_delay_mode", "smart")

    if not links or not acc_ids:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    # Проверка давления инфраструктуры
    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    if warn:
        await callback.answer(warn, show_alert=False)
    else:
        await callback.answer()

    params = {"links": links, "account_ids": acc_ids, "delay_mode": delay_mode}
    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "bulk_join",
            params,
            total_items=len(links) * len(acc_ids),
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
    await safe_edit(
        callback,
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: 🔗 Массовый join\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Каналов: <b>{len(links)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n\n"
        f"Воркер запустит её автоматически.",
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
        await safe_edit(
            callback,
            "🔒 <b>Массовый leave — 💎 ПОДПИСКА</b>\n\nОформите подписку: /subscription",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await state.set_state(BulkLeaveFSM.waiting_channels)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    await safe_edit(
        callback,
        "🚪 <b>Массовый leave — Шаг 1/4</b>\n\n"
        "Введите юзернеймы или ID каналов/групп — <b>по одному на строку</b>:\n\n"
        "<code>@channel_name\n"
        "-1001234567890\n"
        "username</code>\n\n"
        "Или <b>загрузите .txt файл</b> со списком каналов.",
        reply_markup=kb.as_markup(),
    )


async def _process_bl_channels(
    channels: list[str], message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Common logic after collecting bulk_leave channels (text or file)."""
    await state.update_data(bl_channels=channels)
    await state.set_state(BulkLeaveFSM.choosing_accounts)
    accounts = await _get_active_accounts(pool, message.from_user.id)
    if not accounts:
        await state.clear()
        from bot.callbacks import AccCb as _AccCb

        kb = InlineKeyboardBuilder()
        kb.button(text="📱 Перейти к аккаунтам", callback_data=_AccCb(action="menu"))
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="menu"))
        kb.adjust(1)
        await message.answer(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для массового leave нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "💡 Добавьте аккаунт в разделе <b>📱 Аккаунты</b>, затем повторите операцию.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    kb.button(
        text="👥 Все активные аккаунты",
        callback_data=MassOpCb(action="bl_accs", op_type="all"),
    )
    for acc in accounts[:10]:
        kb.button(
            text=f"👤 {_acc_label(acc)}",
            callback_data=MassOpCb(action="bl_accs", op_id=acc["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🚪 <b>Массовый leave — Шаг 2/4</b>\n\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n\n"
        "Выберите аккаунты для выхода:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkLeaveFSM.waiting_channels, F.text)
async def fsm_bulk_leave_channels(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    raw = message.text or ""
    channels = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not channels:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
        await message.answer(
            "⚠️ Введите хотя бы один юзернейм или ID канала:",
            reply_markup=kb.as_markup(),
        )
        return
    if len(channels) > 200:
        channels = channels[:200]
    await _process_bl_channels(channels, message, state, pool)


@router.message(BulkLeaveFSM.waiting_channels, F.document)
async def fsm_bulk_leave_channels_file(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    doc = message.document
    if not doc or (doc.mime_type and not doc.mime_type.startswith("text")):
        await message.answer("⚠️ Отправьте текстовый файл (.txt) со списком каналов.")
        return
    if doc.file_size and doc.file_size > 100_000:
        await message.answer("⚠️ Файл слишком большой. Максимум 100 КБ.")
        return
    try:
        file_info = await message.bot.get_file(doc.file_id)
        downloaded = await message.bot.download_file(file_info.file_path)
        raw = downloaded.read().decode("utf-8", errors="ignore")
    except Exception as e:
        await message.answer(f"⚠️ Не удалось прочитать файл: {e}")
        return
    channels = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not channels:
        await message.answer("⚠️ Файл пустой или не содержит каналов.")
        return
    if len(channels) > 200:
        channels = channels[:200]
        await message.answer("⚠️ Взяты первые 200 строк из файла.")
    await _process_bl_channels(channels, message, state, pool)


@router.callback_query(MassOpCb.filter(F.action == "bl_accs"))
async def cb_bulk_leave_accs(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    if not channels:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    uid = callback.from_user.id
    bl_acc_list_preview = ""
    if callback_data.op_type == "all":
        accounts = await _get_active_accounts(pool, uid)
        acc_ids = [a["id"] for a in accounts]
        acc_names = [_acc_label(a) for a in accounts[:5]]
        acc_label = f"все ({len(acc_ids)})"
        bl_acc_list_preview = "\n".join(f"  👤 {html.escape(n)}" for n in acc_names)
        if len(acc_ids) > 5:
            bl_acc_list_preview += f"\n  … и ещё {len(acc_ids) - 5}"
    else:
        acc_ids = [callback_data.op_id]
        try:
            acc = await pool.fetchrow(
                "SELECT phone, first_name FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                callback_data.op_id,
                uid,
            )
        except Exception:
            acc = None
        acc_label = acc["phone"] if acc else f"id{callback_data.op_id}"
        bl_acc_list_preview = f"  👤 {html.escape(acc_label)}"

    if not acc_ids:
        await callback.answer("Нет активных аккаунтов", show_alert=True)
        return
    await callback.answer()

    ch_preview = "\n".join(f"• {html.escape(ch)}" for ch in channels[:5])
    if len(channels) > 5:
        ch_preview += f"\n… и ещё {len(channels) - 5}"

    await state.update_data(bl_acc_ids=acc_ids, bl_acc_label=acc_label)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚡ Быстро (45-90с)",
        callback_data=MassOpCb(action="bl_delay", op_type="fast"),
    )
    kb.button(
        text="🛡 Нормально (30-75с)",
        callback_data=MassOpCb(action="bl_delay", op_type="normal"),
    )
    kb.button(
        text="🐌 Медленно (60-120с)",
        callback_data=MassOpCb(action="bl_delay", op_type="slow"),
    )
    kb.button(
        text="🧠 Умный (авто)",
        callback_data=MassOpCb(action="bl_delay", op_type="smart"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    bl_acc_section = (
        f"\n<b>Аккаунты:</b>\n{bl_acc_list_preview}" if bl_acc_list_preview else ""
    )
    await safe_edit(
        callback,
        f"🚪 <b>Массовый leave — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>{bl_acc_section}\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n\n"
        f"<b>Список каналов:</b>\n{ch_preview}\n\n"
        f"Выберите режим задержки между выходами:",
        reply_markup=kb.as_markup(),
    )


_DELAY_LABELS_LEAVE = {
    "fast": ("⚡ Быстро", "45–90с", "7–25 мин"),
    "normal": ("🛡 Нормально", "30–75с", "5–20 мин"),
    "slow": ("🐌 Медленно", "60–120с", "10–30 мин"),
    "smart": ("🧠 Умный", "авто", "переменно"),
}


@router.callback_query(MassOpCb.filter(F.action == "bl_redelay"))
async def cb_bulk_leave_redelay(callback: CallbackQuery, state: FSMContext) -> None:
    """Вернуться к выбору задержки в bulk_leave (сохраняя выбранные аккаунты)."""
    await callback.answer()
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    acc_label = sd.get("bl_acc_label", "?")

    ch_preview = "\n".join(f"• {html.escape(ch)}" for ch in channels[:5])
    if len(channels) > 5:
        ch_preview += f"\n… и ещё {len(channels) - 5}"

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚡ Быстро (45-90с)",
        callback_data=MassOpCb(action="bl_delay", op_type="fast"),
    )
    kb.button(
        text="🛡 Нормально (30-75с)",
        callback_data=MassOpCb(action="bl_delay", op_type="normal"),
    )
    kb.button(
        text="🐌 Медленно (60-120с)",
        callback_data=MassOpCb(action="bl_delay", op_type="slow"),
    )
    kb.button(
        text="🧠 Умный (авто)",
        callback_data=MassOpCb(action="bl_delay", op_type="smart"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(2, 2, 1)
    await safe_edit(
        callback,
        f"🚪 <b>Массовый leave — Шаг 3/4</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n\n"
        f"<b>Список:</b>\n{ch_preview}\n\n"
        f"Выберите режим задержки между выходами:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bl_delay"))
async def cb_bulk_leave_delay(
    callback: CallbackQuery,
    callback_data: MassOpCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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

    icon, delay_str, time_est = _DELAY_LABELS_LEAVE.get(
        delay_mode, ("🧠 Умный", "авто", "переменно")
    )
    n = len(channels) * len(acc_ids)
    cap_line, intel = await asyncio.gather(
        _capacity_line(pool, callback.from_user.id, "leave", n, acc_ids),
        _intel_block(pool, callback.from_user.id, "leave", n, acc_ids),
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить leave", callback_data=MassOpCb(action="bl_confirm"))
    kb.button(text="◀️ Изменить задержку", callback_data=MassOpCb(action="bl_redelay"))
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)
    await safe_edit(
        callback,
        f"🚪 <b>Массовый leave — Шаг 4/4 (Подтверждение)</b>\n\n"
        f"Аккаунты: <b>{acc_label}</b>\n"
        f"Каналов/групп: <b>{len(channels)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n"
        f"Операций: <b>{n}</b> (~{time_est})\n"
        + (f"{cap_line}\n" if cap_line else "")
        + (f"\n{intel}\n" if intel else "")
        + f"\n<b>Список:</b>\n{ch_preview}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(MassOpCb.filter(F.action == "bl_confirm"))
async def cb_bulk_leave_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    channels = sd.get("bl_channels", [])
    acc_ids = sd.get("bl_acc_ids", [])
    delay_mode = sd.get("bl_delay_mode", "smart")

    if not channels or not acc_ids:
        await callback.answer("Сессия устарела. Начните заново.", show_alert=True)
        await state.clear()
        return

    # Проверка давления инфраструктуры
    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    if warn:
        await callback.answer(warn, show_alert=False)
    else:
        await callback.answer()

    params = {"channels": channels, "account_ids": acc_ids, "delay_mode": delay_mode}
    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "bulk_leave",
            params,
            total_items=len(channels) * len(acc_ids),
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
    await safe_edit(
        callback,
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: 🚪 Массовый leave\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"Задержка: <b>{icon} {delay_str}</b>\n\n"
        f"Воркер запустит её автоматически.",
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
    await safe_edit(
        callback,
        "🛠️ <b>Построитель операций</b>\n\n"
        "Шаг 1/4: Выберите тип операции\n\n"
        "📤 <b>Публикация</b> — разослать пост по каналам\n"
        "🔗 <b>Join</b> — вступить в каналы (💎 подписка)\n"
        "🚪 <b>Leave</b> — выйти из каналов (💎 подписка)\n"
        "✏️ <b>Редактирование ботов</b> — обновить профиль ботов (PRO)\n",
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
        plan_label = "💎 ПОДПИСКА"
        await safe_edit(
            callback,
            f"🔒 <b>{meta['label']} — {plan_label}</b>\n\nОформите подписку: /subscription",
            reply_markup=subscription_locked_markup(
                required_plan, back_callback=MassOpCb(action="build")
            ),
        )
        return

    await callback.answer()
    await state.update_data(ob_op_type=op_type)
    await state.set_state(OpBuilderFSM.choosing_targets)

    kb = InlineKeyboardBuilder()

    if op_type == "mass_publish":
        kb.button(
            text="📢 Каналы",
            callback_data=MassOpCb(action="ob_target", op_type="channels"),
        )
        kb.button(
            text="👥 Группы",
            callback_data=MassOpCb(action="ob_target", op_type="groups"),
        )
        kb.button(
            text="📢+👥 Каналы и группы",
            callback_data=MassOpCb(action="ob_target", op_type="both"),
        )
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="build"))
        kb.adjust(2, 1, 1)
        await safe_edit(
            callback,
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            "Шаг 2/4: Выберите тип целей для публикации:",
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
        await safe_edit(
            callback,
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            f"Шаг 2/4: Введите каналы/группы для {action_word} — по одному на строку:\n\n"
            f"{example}\n\n"
            "Максимум 50 записей.",
            reply_markup=kb.as_markup(),
        )

    elif op_type == "bulk_bot_edit":
        kb.button(
            text="✏️ Имя бота",
            callback_data=MassOpCb(action="ob_target", op_type="name"),
        )
        kb.button(
            text="📄 Описание",
            callback_data=MassOpCb(action="ob_target", op_type="desc"),
        )
        kb.button(
            text="📝 Краткое описание",
            callback_data=MassOpCb(action="ob_target", op_type="short_desc"),
        )
        kb.button(
            text="⌨️ Команды",
            callback_data=MassOpCb(action="ob_target", op_type="commands"),
        )
        kb.button(text="◀️ Назад", callback_data=MassOpCb(action="build"))
        kb.adjust(2, 2, 1)
        await safe_edit(
            callback,
            f"🛠️ <b>Построитель: {meta['icon']} {meta['label']}</b>\n\n"
            "Шаг 2/4: Выберите поле для массового редактирования:",
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
        await safe_edit(
            callback,
            f"🛠️ <b>Построитель: {meta.get('icon', '')} {meta.get('label', '')}</b>\n"
            f"Цели: <b>{target_label}</b>\n\n"
            "Шаг 3/4: Введите текст поста (поддерживается HTML):",
            reply_markup=kb.as_markup(),
        )
    elif op_type == "bulk_bot_edit":
        _FIELD_LABELS_OB = {
            "name": "имя бота",
            "desc": "описание",
            "short_desc": "краткое описание",
            "commands": "команды (формат: /cmd - описание, по одному на строку)",
        }
        field_label = _FIELD_LABELS_OB.get(target, target)
        await safe_edit(
            callback,
            f"🛠️ <b>Построитель: {meta.get('icon', '')} {meta.get('label', '')}</b>\n"
            f"Поле: <b>{field_label}</b>\n\n"
            f"Шаг 3/4: Введите новое значение для всех ботов:",
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


async def _ob_show_preview(
    msg,
    state: FSMContext,
    pool: asyncpg.Pool,
    meta: dict,
    op_type: str,
    edit: bool = False,
) -> None:
    """Показать preview и кнопку подтверждения."""
    sd = await state.get_data()
    target = sd.get("ob_target", "")
    ob_param = sd.get("ob_param", "")
    ob_links = sd.get("ob_links", [])

    target_label = (
        _TARGET_LABELS.get(target, target) if op_type == "mass_publish" else target
    )

    # Считаем количество аккаунтов/целей
    uid = msg.from_user.id if hasattr(msg, "from_user") else msg.chat.id
    acc_count = 0
    try:
        accounts = await _get_active_accounts(pool, uid)
        acc_count = len(accounts)
    except Exception:
        log_exc_swallow(
            log, "Не удалось посчитать активные аккаунты для предпросмотра операции"
        )

    lines = []
    lines.append("🛠️ <b>Построитель — Предпросмотр операции</b>")
    lines.append("")
    lines.append(f"Тип: {meta.get('icon', '')} <b>{meta.get('label', op_type)}</b>")

    if op_type == "mass_publish":
        lines.append(f"Цели: <b>{target_label}</b>")
        preview_text = html.escape(ob_param[:200])
        lines.append(f"Аккаунтов: <b>{acc_count}</b>")
        # Estimate channel count for ETA
        target_filter = sd.get("ob_target", "")
        chan_count = 0
        try:
            if target_filter in ("channels", "both"):
                chan_count += (
                    await pool.fetchval(
                        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid
                    )
                    or 0
                )
            if target_filter in ("groups", "both"):
                chan_count += (
                    await pool.fetchval(
                        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid
                    )
                    or 0
                )
        except Exception:
            log_exc_swallow(log, "_ob_show_preview: channel count failed")
        if chan_count > 0:
            # ~30s per publish with safe delays
            eta_secs = chan_count * 30
            eta_min = eta_secs // 60
            eta_str = f"~{eta_min}м" if eta_min > 0 else "<1м"
            lines.append(
                f"Каналов/групп: <b>{chan_count}</b> | ⏱️ ETA: <b>{eta_str}</b>"
            )
        lines.append(f"\nТекст поста:\n<i>{preview_text}</i>")
    elif op_type in ("bulk_join", "bulk_leave"):
        action_word = "вступления" if op_type == "bulk_join" else "выхода"
        link_preview = "\n".join(f"• {html.escape(ln)}" for ln in ob_links[:5])
        if len(ob_links) > 5:
            link_preview += f"\n… и ещё {len(ob_links) - 5}"
        lines.append(f"Каналов/групп: <b>{len(ob_links)}</b>")
        lines.append(f"Аккаунтов для {action_word}: <b>{acc_count}</b>")
        # ETA: accounts × targets × ~60s per action
        if acc_count > 0 and ob_links:
            eta_secs = acc_count * len(ob_links) * 60
            eta_min = eta_secs // 60
            eta_h = eta_min // 60
            eta_str = f"~{eta_h}ч {eta_min % 60}м" if eta_h else f"~{eta_min}м"
            lines.append(f"⏱️ Примерное время: <b>{eta_str}</b> (safe режим)")
        lines.append(f"\n<b>Список:</b>\n{link_preview}")
    elif op_type == "bulk_bot_edit":
        _FIELD_LABELS_OB = {
            "name": "Имя",
            "desc": "Описание",
            "short_desc": "Краткое описание",
            "commands": "Команды",
        }
        field_label = _FIELD_LABELS_OB.get(target, target)
        preview_val = html.escape(ob_param[:200])
        lines.append(f"Поле: <b>{field_label}</b>")
        lines.append(f"\nЗначение:\n<i>{preview_val}</i>")

    lines.append("")
    lines.append("Шаг 4/4: Подтвердить запуск операции?")

    preview_text_full = "\n".join(lines)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Подтвердить и поставить в очередь",
        callback_data=MassOpCb(action="ob_confirm"),
    )
    kb.button(text="❌ Отмена", callback_data=MassOpCb(action="menu"))
    kb.adjust(1)

    if edit:
        try:
            await msg.edit_text(
                preview_text_full, parse_mode="HTML", reply_markup=kb.as_markup()
            )
            return
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось отредактировать предпросмотр построителя операций, отправляем новое",
            )
    await msg.answer(preview_text_full, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Шаг 4: Подтверждение и запись в operation_queue ──────────────────────


@router.callback_query(MassOpCb.filter(F.action == "ob_confirm"))
async def cb_ob_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    # Проверка давления инфраструктуры
    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    await callback.answer(warn or "⏳ Создаю операцию...", show_alert=bool(warn))

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
            "delay_seconds": 30,  # op_worker uses "delay_seconds" key
            "delay": 30,  # backward-compat alias
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
        await safe_edit(
            callback,
            "⚠️ Неизвестный тип операции.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    try:
        op_id = await operation_bus.submit(
            pool,
            uid,
            op_type,
            params,
            total_items=total_items,
        )
    except Exception as e:
        log.error("ob_confirm insert error: %s", e)
        await safe_edit(
            callback,
            "⚠️ Ошибка создания операции. Попробуйте ещё раз.",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    icon = meta.get("icon", "")
    label = meta.get("label", op_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue"))
    kb.button(text="◀️ Меню", callback_data=MassOpCb(action="menu"))
    kb.adjust(2)
    await safe_edit(
        callback,
        f"✅ <b>Операция #{op_id} поставлена в очередь</b>\n\n"
        f"Тип: {icon} <b>{label}</b>\n"
        f"Статус: ⏳ Ожидает выполнения\n\n"
        f"Воркер запустит операцию автоматически.\n"
        f"Следить за прогрессом: <b>Очередь операций</b>",
        reply_markup=kb.as_markup(),
    )
