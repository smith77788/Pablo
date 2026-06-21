"""Growth Agent Hub — Autonomous Growth Agent UI.

Пользователь ставит цель (например, '+10K подписчиков за 30 дней'),
AI строит стратегию, выполняет операции и ежедневно корректирует курс.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, GrowthCb
from bot.states import GrowthAgentFSM
from services import growth_agent

log = logging.getLogger(__name__)
router = Router(name="growth_agent_hub")

# ── Метрики ───────────────────────────────────────────────────────────────────

_METRICS: dict[str, tuple[str, str]] = {
    "subscribers": ("👥", "Подписчики"),
    "views":       ("👁", "Просмотры"),
    "revenue_usd": ("💵", "Доход (USD)"),
    "members":     ("🏘", "Участники группы"),
    "reactions":   ("❤️", "Реакции"),
    "reposts":     ("🔁", "Репосты"),
}

_STATUS_ICONS: dict[str, str] = {
    "active":    "🟢",
    "paused":    "⏸",
    "completed": "✅",
    "failed":    "❌",
}

_OUTCOME_ICONS: dict[str, str] = {
    "queued":  "🔄",
    "success": "✅",
    "failed":  "❌",
    "skipped": "⏭",
}


# ── helpers ───────────────────────────────────────────────────────────────────


def _metric_label(metric: str) -> str:
    icon, label = _METRICS.get(metric, ("📊", metric))
    return f"{icon} {label}"


def _status_label(status: str) -> str:
    icon = _STATUS_ICONS.get(status, "❓")
    labels = {
        "active":    "Активна",
        "paused":    "Пауза",
        "completed": "Завершена",
        "failed":    "Провалена",
    }
    return f"{icon} {labels.get(status, status)}"


def _progress_bar(pct: float, width: int = 10) -> str:
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:.1f}%"


def _goals_kb(goals: list[dict], page: int = 0) -> object:
    kb = InlineKeyboardBuilder()
    for g in goals:
        status_icon = _STATUS_ICONS.get(g["status"], "❓")
        target = int(g["target_value"] or 1)
        current = int(g["current_value"] or 0)
        pct = min(100.0, current / target * 100)
        short_desc = html.escape(g["description"][:35])
        kb.button(
            text=f"{status_icon} {short_desc}… {pct:.0f}%",
            callback_data=GrowthCb(action="detail", goal_id=g["id"]),
        )
    kb.button(text="➕ Новая цель", callback_data=GrowthCb(action="create"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _detail_kb(goal_id: int, status: str) -> object:
    kb = InlineKeyboardBuilder()
    if status == "active":
        kb.button(text="⏸ Поставить на паузу", callback_data=GrowthCb(action="pause", goal_id=goal_id))
    elif status == "paused":
        kb.button(text="▶️ Возобновить", callback_data=GrowthCb(action="resume", goal_id=goal_id))
    kb.button(text="🗑 Удалить", callback_data=GrowthCb(action="confirm_delete", goal_id=goal_id))
    kb.button(text="◀️ К целям", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def _confirm_delete_kb(goal_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Да, удалить", callback_data=GrowthCb(action="delete", goal_id=goal_id))
    kb.button(text="↩️ Отмена", callback_data=GrowthCb(action="detail", goal_id=goal_id))
    kb.adjust(2)
    return kb.as_markup()


def _metrics_kb() -> object:
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _METRICS.items():
        kb.button(text=f"{icon} {label}", callback_data=f"ga_metric:{key}")
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(2)
    return kb.as_markup()


def _cancel_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


async def _send_goal_list(pool: asyncpg.Pool, target: Message | CallbackQuery, owner_id: int) -> None:
    try:
        goals = await growth_agent.list_goals(pool, owner_id, limit=20)
    except Exception as e:
        log.error("growth_agent_hub._send_goal_list: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
        text = (
            "🤖 <b>Autonomous Growth Agent</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v114.sql</code>."
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
        return

    lines = ["🤖 <b>Autonomous Growth Agent</b>\n"]
    if not goals:
        lines.append("У вас пока нет целей роста.\nНажмите <b>Новая цель</b>, чтобы начать.")
    else:
        lines.append(f"<b>Целей:</b> {len(goals)}\n")
        for g in goals:
            target_val = int(g["target_value"])
            current_val = int(g["current_value"] or 0)
            pct = min(100.0, current_val / max(target_val, 1) * 100)
            status = _status_label(g["status"])
            metric = _metric_label(g["target_metric"])
            lines.append(
                f"• {status} | {metric}: {current_val}/{target_val} ({pct:.0f}%)\n"
                f"  {html.escape(g['description'][:60])}"
            )

    text = "\n".join(lines)
    markup = _goals_kb(goals)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


# ── Entry points ──────────────────────────────────────────────────────────────


@router.callback_query(GrowthCb.filter(F.action == "menu"))
async def cb_growth_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await _send_goal_list(pool, callback, callback.from_user.id)


@router.message(Command("growth"))
async def cmd_growth(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    await _send_goal_list(pool, message, message.from_user.id)


# ── Goal detail (shared helper + callback) ────────────────────────────────────


async def _show_goal_detail(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    goal_id: int,
) -> None:
    """Отрисовать детали цели в текущем сообщении."""
    owner_id = callback.from_user.id
    status_data = await growth_agent.get_goal_status(pool, goal_id)
    if not status_data:
        await callback.message.edit_text(
            "❌ Цель не найдена.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    if int(status_data["owner_id"]) != owner_id:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    pct = status_data["progress_pct"]
    current = int(status_data["current_value"] or 0)
    target = int(status_data["target_value"])
    metric = _metric_label(status_data["target_metric"])
    status = _status_label(status_data["status"])
    days_left = status_data.get("days_left")
    desc = html.escape(status_data["description"])

    lines = [
        f"🎯 <b>Цель роста #{goal_id}</b>",
        f"{desc}",
        "",
        f"📊 <b>Метрика:</b> {metric}",
        f"📈 <b>Прогресс:</b> {_progress_bar(pct)}",
        f"   {current} / {target}",
        f"🏷 <b>Статус:</b> {status}",
    ]

    if days_left is not None:
        lines.append(f"⏰ <b>Осталось дней:</b> {days_left}")

    deadline = status_data.get("deadline_at")
    if deadline and hasattr(deadline, "strftime"):
        lines.append(f"📅 <b>Дедлайн:</b> {deadline.strftime('%d.%m.%Y')}")

    strategy = status_data.get("strategy", "balanced")
    lines.append(f"🔧 <b>Стратегия:</b> <code>{strategy}</code>")

    recent = status_data.get("recent_actions", [])
    if recent:
        lines.append("\n<b>📋 Последние 5 действий:</b>")
        for act in recent:
            icon = _OUTCOME_ICONS.get(act.get("outcome", ""), "•")
            atype = html.escape(str(act.get("action_type", "")))
            adesc = html.escape(str(act.get("description", ""))[:60])
            executed = act.get("executed_at")
            ts = ""
            if executed and hasattr(executed, "strftime"):
                ts = executed.strftime(" %d.%m %H:%M")
            lines.append(f"  {icon} <code>{atype}</code> — {adesc}{ts}")

    last_report = status_data.get("last_report")
    if last_report:
        lines.append("\n<b>📝 Последний отчёт:</b>")
        commentary = html.escape(str(last_report.get("ai_commentary", "")))
        lines.append(f"  {commentary}")

    if days_left is not None and days_left > 0 and pct > 0 and pct < 100 and current > 0:
        days_needed = round((target - current) / max(1, current) * days_left)
        if days_needed < days_left:
            lines.append(f"\n🔮 <b>Прогноз:</b> достижимо за ~{days_needed} дн. (осталось {days_left} дн.)")
        else:
            lines.append(f"\n⚠️ <b>Прогноз:</b> может не уложиться в дедлайн (нужно ~{days_needed} дн.)")

    text = "\n".join(lines)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_detail_kb(goal_id, status_data["status"]),
    )


@router.callback_query(GrowthCb.filter(F.action == "detail"))
async def cb_growth_detail(
    callback: CallbackQuery,
    callback_data: GrowthCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await _show_goal_detail(callback, pool, callback_data.goal_id)


# ── Pause / Resume / Delete ───────────────────────────────────────────────────


@router.callback_query(GrowthCb.filter(F.action == "pause"))
async def cb_growth_pause(
    callback: CallbackQuery,
    callback_data: GrowthCb,
    pool: asyncpg.Pool,
) -> None:
    ok = await growth_agent.pause_goal(pool, callback_data.goal_id, callback.from_user.id)
    await callback.answer(
        "⏸ Цель поставлена на паузу." if ok else "❌ Не удалось поставить на паузу.",
        show_alert=True,
    )
    await _show_goal_detail(callback, pool, callback_data.goal_id)


@router.callback_query(GrowthCb.filter(F.action == "resume"))
async def cb_growth_resume(
    callback: CallbackQuery,
    callback_data: GrowthCb,
    pool: asyncpg.Pool,
) -> None:
    ok = await growth_agent.resume_goal(pool, callback_data.goal_id, callback.from_user.id)
    await callback.answer(
        "▶️ Цель возобновлена." if ok else "❌ Не удалось возобновить.",
        show_alert=True,
    )
    await _show_goal_detail(callback, pool, callback_data.goal_id)


@router.callback_query(GrowthCb.filter(F.action == "confirm_delete"))
async def cb_growth_confirm_delete(
    callback: CallbackQuery,
    callback_data: GrowthCb,
) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "⚠️ <b>Удалить цель?</b>\n\nВсе действия и отчёты по этой цели будут удалены. Это необратимо.",
        parse_mode="HTML",
        reply_markup=_confirm_delete_kb(callback_data.goal_id),
    )


@router.callback_query(GrowthCb.filter(F.action == "delete"))
async def cb_growth_delete(
    callback: CallbackQuery,
    callback_data: GrowthCb,
    pool: asyncpg.Pool,
) -> None:
    ok = await growth_agent.delete_goal(pool, callback_data.goal_id, callback.from_user.id)
    if ok:
        await callback.answer("🗑 Цель удалена.", show_alert=True)
    else:
        await callback.answer("❌ Не удалось удалить цель.", show_alert=True)
    await _send_goal_list(pool, callback, callback.from_user.id)


# ── Create goal FSM ───────────────────────────────────────────────────────────


@router.callback_query(GrowthCb.filter(F.action == "create"))
async def cb_growth_create(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(GrowthAgentFSM.waiting_description)
    await callback.message.edit_text(
        "🤖 <b>Новая цель роста</b>\n\n"
        "Опишите вашу цель в свободной форме.\n\n"
        "<b>Примеры:</b>\n"
        "• +10K подписчиков в нише крипты за 30 дней\n"
        "• Набрать 50K просмотров на канале за 2 недели\n"
        "• Вырасти до 1000 членов группы за месяц\n\n"
        "✍️ Введите описание:",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(GrowthAgentFSM.waiting_description)
async def fsm_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if len(description) < 5:
        await message.answer(
            "❌ Описание слишком короткое. Попробуйте ещё раз:",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(description=description)
    await state.set_state(GrowthAgentFSM.waiting_metric)
    await message.answer(
        "📊 <b>Выберите метрику цели:</b>",
        parse_mode="HTML",
        reply_markup=_metrics_kb(),
    )


@router.callback_query(F.data.startswith("ga_metric:"))
async def cb_pick_metric(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    current_state = await state.get_state()
    if current_state != GrowthAgentFSM.waiting_metric:
        await callback.answer("Сначала начните создание цели.", show_alert=True)
        return

    metric = callback.data.split(":", 1)[1]
    if metric not in _METRICS:
        await callback.answer("❌ Неверная метрика.", show_alert=True)
        return

    await state.update_data(metric=metric)
    await state.set_state(GrowthAgentFSM.waiting_target)

    icon, label = _METRICS[metric]
    await callback.message.edit_text(
        f"🎯 <b>Метрика:</b> {icon} {label}\n\n"
        f"Введите целевое значение (только число).\n"
        f"<i>Например: 10000</i>",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(GrowthAgentFSM.waiting_target)
async def fsm_target(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip().replace(" ", "").replace(",", "")
    try:
        target_value = int(text)
        if target_value <= 0:
            raise ValueError("must be positive")
    except ValueError:
        await message.answer(
            "❌ Введите целое положительное число.\n<i>Например: 10000</i>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(target_value=target_value)
    await state.set_state(GrowthAgentFSM.waiting_deadline)
    await message.answer(
        "⏰ <b>Дедлайн</b>\n\n"
        "Через сколько дней вы хотите достичь цели?\n"
        "<i>Введите число от 1 до 365</i>",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(GrowthAgentFSM.waiting_deadline)
async def fsm_deadline(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    try:
        deadline_days = int(text)
        if not (1 <= deadline_days <= 365):
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ Введите число от 1 до 365.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(deadline_days=deadline_days)
    await state.set_state(GrowthAgentFSM.confirming)

    data = await state.get_data()
    metric_key = data.get("metric", "subscribers")
    icon, metric_label = _METRICS.get(metric_key, ("📊", metric_key))
    description = html.escape(data.get("description", ""))
    target_value = data.get("target_value", 0)

    deadline_date = (
        datetime.now(timezone.utc).date()
        if deadline_days == 0
        else (datetime.now(timezone.utc) + timedelta(days=deadline_days)).date()
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать цель", callback_data=GrowthCb(action="confirm_create"))
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(2)

    await message.answer(
        f"📋 <b>Подтверждение новой цели</b>\n\n"
        f"📝 <b>Описание:</b> {description}\n"
        f"📊 <b>Метрика:</b> {icon} {metric_label}\n"
        f"🎯 <b>Таргет:</b> {target_value:,}\n"
        f"⏰ <b>Дедлайн:</b> {deadline_days} дней ({deadline_date})\n\n"
        f"Создать эту цель?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GrowthCb.filter(F.action == "confirm_create"))
async def cb_growth_confirm_create(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()

    description = data.get("description", "")
    metric = data.get("metric", "subscribers")
    target_value = data.get("target_value", 0)
    deadline_days = data.get("deadline_days", 30)
    owner_id = callback.from_user.id

    if not description or not target_value:
        await callback.message.edit_text(
            "❌ Данные формы устарели. Начните создание заново.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    try:
        goal_id = await growth_agent.create_goal(
            pool=pool,
            owner_id=owner_id,
            description=description,
            target_metric=metric,
            target_value=int(target_value),
            deadline_days=int(deadline_days),
        )
    except Exception as exc:
        log.error("growth_agent_hub: create_goal failed owner=%d: %s", owner_id, exc)
        await callback.message.edit_text(
            f"❌ Ошибка при создании цели: {html.escape(str(exc))}",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    icon, metric_label = _METRICS.get(metric, ("📊", metric))
    await callback.message.edit_text(
        f"✅ <b>Цель #{goal_id} создана!</b>\n\n"
        f"🤖 Growth Agent начнёт работу в ближайшем цикле (каждые 6 часов).\n\n"
        f"📊 <b>Метрика:</b> {icon} {metric_label}\n"
        f"🎯 <b>Таргет:</b> {int(target_value):,}\n"
        f"⏰ <b>Дедлайн:</b> {deadline_days} дней",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(text="🔍 Детали цели", callback_data=GrowthCb(action="detail", goal_id=goal_id))
        .button(text="◀️ К целям", callback_data=GrowthCb(action="menu"))
        .adjust(1)
        .as_markup(),
    )
