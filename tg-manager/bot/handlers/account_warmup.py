"""
Account Warmup UI — управление планами разогрева аккаунтов.

Разогрев нужен для новых аккаунтов: имитирует натуральную активность
перед боевыми операциями, повышает trust_score, снижает риск блокировок.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import WarmupCb, BmCb

log = logging.getLogger(__name__)
router = Router()

_PLAN_LABELS = {
    "gentle": "🌱 Gentle (21 день, 5 действий/день)",
    "standard": "🌿 Standard (14 дней, 10 действий/день)",
    "aggressive": "🔥 Aggressive (7 дней, 20 действий/день)",
}


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    return kb


# ── Меню разогрева ────────────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "menu"))
async def cb_warmup_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.account_warmer import get_active_plans

    plans = await get_active_plans(pool, callback.from_user.id)
    active_count = len(plans)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Создать план разогрева", callback_data=WarmupCb(action="create_list")
    )
    kb.button(text="📋 Активные планы", callback_data=WarmupCb(action="active_plans"))
    kb.button(text="▶️ Запустить сейчас", callback_data=WarmupCb(action="run_now"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🌡 <b>Account Warming — Разогрев аккаунтов</b>\n\n"
        "Постепенный разогрев новых аккаунтов перед боевыми операциями.\n"
        "Система реально выполняет: чтение каналов, реакции на посты, "
        "поиск, просмотр профилей, вступление в каналы.\n\n"
        f"Активных планов: <b>{active_count}</b>\n\n"
        "<b>Режимы:</b>\n"
        "🌱 Gentle — 21 день, 5 действий/день\n"
        "🌿 Standard — 14 дней, 10 действий/день\n"
        "🔥 Aggressive — 7 дней, 20 действий/день\n\n"
        "⚙️ Прогрев запускается автоматически раз в сутки",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Список аккаунтов для создания плана ──────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "create_list"))
async def cb_warmup_create_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    accounts = await pool.fetch(
        """SELECT a.id, a.phone, a.first_name,
                  COALESCE(a.acc_status, 'active') AS acc_status,
                  EXISTS(SELECT 1 FROM account_warmup_plans wp
                         WHERE wp.account_id=a.id AND wp.status='active') AS has_plan
           FROM tg_accounts a
           WHERE a.owner_id=$1 AND a.is_active=TRUE
           ORDER BY a.added_at DESC""",
        callback.from_user.id,
    )

    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет доступных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    total = len(accounts)
    no_plan_count = sum(1 for acc in accounts if not acc["has_plan"])

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🌡 Выбрать все аккаунты ({total})",
        callback_data=WarmupCb(action="select_all_plan"),
    )
    for acc in accounts:
        icon = "✅" if acc["has_plan"] else "⚪"
        label = acc.get("first_name") or acc["phone"]
        kb.button(
            text=f"{icon} {html.escape(label)} [{acc['acc_status']}]",
            callback_data=WarmupCb(action="select_plan", account_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    no_plan_str = f"\n⚪ Без плана: <b>{no_plan_count}</b>" if no_plan_count > 0 else ""
    await callback.message.edit_text(
        f"📱 <b>Выберите аккаунт для разогрева:</b>\n\n"
        f"✅ = уже есть активный план\n"
        f"⚪ = план отсутствует"
        f"{no_plan_str}\n\n"
        "Нажмите <b>«Выбрать все»</b> чтобы создать план для всех аккаунтов сразу.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "select_plan"))
async def cb_warmup_select_plan(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.account_id

    kb = InlineKeyboardBuilder()
    for plan_key, plan_label in _PLAN_LABELS.items():
        kb.button(
            text=plan_label,
            callback_data=WarmupCb(action=f"plan_{plan_key}", account_id=acc_id),
        )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="create_list"))
    kb.adjust(1)

    acc = await pool.fetchrow(
        "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
    )
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    await callback.message.edit_text(
        f"🌡 <b>Разогрев: {html.escape(label)}</b>\n\nВыберите режим разогрева:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "select_all_plan"))
async def cb_warmup_select_all_plan(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    count = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        callback.from_user.id,
    )

    kb = InlineKeyboardBuilder()
    for plan_key, plan_label in _PLAN_LABELS.items():
        kb.button(
            text=plan_label,
            callback_data=WarmupCb(action=f"pall_{plan_key}"),
        )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="create_list"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"🌡 <b>Разогрев всех аккаунтов ({count} шт.)</b>\n\n"
        "Выберите режим разогрева для <b>всех</b> аккаунтов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(F.action.in_({"pall_gentle", "pall_standard", "pall_aggressive"}))
)
async def cb_warmup_create_all_plans(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю планы...")
    from services.account_warmer import create_warmup_plan

    plan_type = callback_data.action.replace("pall_", "")
    user_id = callback.from_user.id

    accounts = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE ORDER BY added_at DESC",
        user_id,
    )

    created = 0
    for acc in accounts:
        try:
            await create_warmup_plan(pool, user_id, acc["id"], plan_type)
            created += 1
        except Exception as exc:
            log.warning("warmup create_all: acc=%d error=%s", acc["id"], exc)

    await callback.message.edit_text(
        f"✅ <b>Планы разогрева созданы!</b>\n\n"
        f"Аккаунтов: <b>{created}/{len(accounts)}</b>\n"
        f"Режим: <b>{_PLAN_LABELS.get(plan_type, plan_type)}</b>\n\n"
        "Разогрев запускается автоматически каждые 6 часов.\n"
        "Или используйте «▶️ Запустить сейчас» для немедленного старта.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(F.action.in_({"plan_gentle", "plan_standard", "plan_aggressive"}))
)
async def cb_warmup_create_plan(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from services.account_warmer import create_warmup_plan

    plan_type = callback_data.action.replace("plan_", "")
    acc_id = callback_data.account_id

    plan_id = await create_warmup_plan(pool, callback.from_user.id, acc_id, plan_type)

    acc = await pool.fetchrow(
        "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
    )
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    await callback.message.edit_text(
        f"✅ <b>План разогрева создан!</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Режим: <b>{_PLAN_LABELS.get(plan_type, plan_type)}</b>\n"
        f"ID плана: <code>{plan_id}</code>\n\n"
        "Разогрев запускается автоматически каждые 6 часов.\n"
        "Или используйте «▶️ Запустить сейчас» для немедленного старта.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Активные планы ────────────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "active_plans"))
async def cb_warmup_active_plans(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.account_warmer import get_active_plans

    plans = await get_active_plans(pool, callback.from_user.id)

    if not plans:
        await callback.message.edit_text(
            "📋 <b>Активных планов нет</b>\n\nСоздайте план через «➕ Создать план разогрева».",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = ["📋 <b>Активные планы разогрева</b>\n"]
    kb = InlineKeyboardBuilder()
    now_utc = datetime.now(timezone.utc)
    for plan in plans:
        label = plan.get("first_name") or plan.get("phone") or str(plan["account_id"])
        current_day = plan["current_day"] or 0
        target_days = max(plan["target_days"] or 1, 1)
        pct = round(current_day / target_days * 100)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)

        # Days remaining ETA
        days_left = max(target_days - current_day, 0)
        eta_str = f"⏳ Осталось: {days_left} дн." if days_left > 0 else "🏁 Завершается"

        # Статус разогрева: активный / завершён
        plan_status_emoji = "🟢" if plan.get("status") == "active" else "🏁"

        # Next session: last_action_at + 24h (warmup runs every ~24h)
        last_run = plan.get("last_action_at")
        if last_run:
            last_run_aware = last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
            next_run = last_run_aware + timedelta(hours=24)
            if next_run > now_utc:
                diff = next_run - now_utc
                diff_h = int(diff.total_seconds() // 3600)
                diff_m = int((diff.total_seconds() % 3600) // 60)
                next_str = f"⏰ Следующий сеанс: через {diff_h}ч {diff_m}м"
            else:
                next_str = "⏰ Следующий сеанс: скоро"
            last_str = f"\n  Последний сеанс: {last_run_aware.strftime('%d.%m %H:%M')}"
        else:
            next_str = "⏰ Следующий сеанс: ещё не запускался"
            last_str = ""

        # Статус trust score (если есть в плане)
        trust_val = plan.get("trust_score")
        trust_str = ""
        if trust_val is not None:
            ts = float(trust_val)
            filled = min(8, round(ts * 8))
            trust_bar = "█" * filled + "░" * (8 - filled)
            trust_str = f"\n  ⭐ Trust: [{trust_bar}] {ts:.2f}"

        lines.append(
            f"{plan_status_emoji} <b>{html.escape(label)}</b>\n"
            f"  [{bar}] День {current_day}/{target_days} ({pct}%)\n"
            f"  Режим: <b>{plan['plan_type']}</b> | {plan['daily_actions']} действий/день\n"
            f"  {eta_str}\n"
            f"  {next_str}"
            f"{last_str}"
            f"{trust_str}"
        )
        # Кнопки: Лог + Запустить сейчас + Остановить
        kb.button(
            text=f"📋 Лог {label[:10]}",
            callback_data=WarmupCb(action="plan_log", plan_id=plan["id"], account_id=plan["account_id"]),
        )
        kb.button(
            text=f"▶️ Запуск {label[:10]}",
            callback_data=WarmupCb(action="run_one", plan_id=plan["id"], account_id=plan["account_id"]),
        )
        kb.button(
            text=f"🗑 Удалить {label[:10]}",
            callback_data=WarmupCb(action="delete_plan", plan_id=plan["id"]),
        )

    kb.button(text="▶️ Запустить все сейчас", callback_data=WarmupCb(action="run_now"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(3)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "delete_plan"))
async def cb_warmup_delete_plan(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await pool.execute(
        "UPDATE account_warmup_plans SET status='cancelled' WHERE id=$1 AND owner_id=$2",
        callback_data.plan_id,
        callback.from_user.id,
    )
    await callback.message.edit_text(
        "🗑 <b>План разогрева отменён</b>",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Запуск прямо сейчас ────────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "run_now"))
async def cb_warmup_run_now(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("▶️ Запускаю в фоне...")
    from services.account_warmer import get_active_plans, run_daily_warmup
    from services import task_registry
    import asyncio

    plans = await get_active_plans(pool, callback.from_user.id)
    if not plans:
        await callback.message.edit_text(
            "⚠️ Нет активных планов разогрева.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    user_id = callback.from_user.id

    async def _run_all():
        for plan in plans:
            label = plan.get("first_name") or plan.get("phone") or str(plan["account_id"])
            try:
                await run_daily_warmup(pool, plan)
            except Exception as exc:
                log.warning("warmup run_all error acc=%s: %s", label, exc)

    task = asyncio.create_task(_run_all())
    task_registry.register(user_id, "warmup", f"Разогрев всех аккаунтов ({len(plans)})", task)

    await callback.message.edit_text(
        f"🌡 <b>Разогрев запущен в фоне</b>\n\n"
        f"Планов: <b>{len(plans)}</b>\n"
        "Процесс займёт время (20-90с между действиями).\n\n"
        "Следите за прогрессом в <b>⚡ Active Tasks</b>.",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Запуск разогрева для конкретного аккаунта ────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "run_one"))
async def cb_warmup_run_one(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    """Запускает один цикл разогрева для выбранного плана в фоне."""
    await callback.answer("▶️ Запускаю в фоне...")
    from services.account_warmer import get_active_plans, run_daily_warmup
    from services import task_registry
    import asyncio

    plan_id = callback_data.plan_id
    acc_id = callback_data.account_id

    plans = await get_active_plans(pool, callback.from_user.id)
    plan = next((p for p in plans if p["id"] == plan_id), None)

    if not plan:
        await callback.message.edit_text(
            "⚠️ План не найден или уже завершён.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    label = plan.get("first_name") or plan.get("phone") or str(acc_id)
    daily = plan.get("daily_actions", 5)
    user_id = callback.from_user.id

    task = asyncio.create_task(run_daily_warmup(pool, plan))
    task_registry.register(user_id, "warmup", f"Разогрев: {label}", task)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Лог разогрева", callback_data=WarmupCb(action="plan_log", plan_id=plan_id, account_id=acc_id))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="active_plans"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"🌡 <b>Разогрев запущен: {html.escape(label)}</b>\n\n"
        f"Действий: <b>{daily}</b>\n"
        "Работает в фоне. Лог обновится после завершения.\n\n"
        "Следите за прогрессом в <b>⚡ Active Tasks</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Warmup plan action log ─────────────────────────────────────────────────────

_ACTION_LABELS = {
    "read_channel":   "📖 Читал канал",
    "join_channel":   "🔔 Вступил в канал",
    "send_reaction":  "❤️ Поставил реакцию",
    "search":         "🔍 Поиск по слову",
    "view_profile":   "👁 Смотрел профиль",
    "open_chat":      "💬 Открыл чат",
    "dm_bot":         "🤖 Написал боту",
    "read_messages":  "📨 Читал сообщения",
}


@router.callback_query(WarmupCb.filter(F.action == "plan_log"))
async def cb_warmup_plan_log(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.account_id
    plan_id = callback_data.plan_id

    # Get account name
    acc_row = await pool.fetchrow(
        "SELECT first_name, phone FROM tg_accounts WHERE id=$1", acc_id
    )
    label = ""
    if acc_row:
        label = acc_row.get("first_name") or acc_row.get("phone") or f"id{acc_id}"

    # Get plan info
    plan_row = await pool.fetchrow(
        "SELECT current_day, target_days, plan_type, daily_actions FROM account_warmup_plans WHERE id=$1",
        plan_id,
    )

    # Get last 50 actions from warmup log
    rows = await pool.fetch(
        """SELECT action_type, target, success, error, performed_at
           FROM account_warmup_log
           WHERE account_id=$1
           ORDER BY performed_at DESC
           LIMIT 30""",
        acc_id,
    )

    lines = [f"📋 <b>Лог разогрева: {html.escape(label)}</b>\n"]

    if plan_row:
        lines.append(
            f"День {plan_row['current_day']}/{plan_row['target_days']} | "
            f"{plan_row['plan_type']} | {plan_row['daily_actions']} действий/день\n"
        )

    if not rows:
        lines.append("Действий ещё не выполнено.")
    else:
        # Group by day
        from collections import defaultdict
        by_day: dict[str, list] = defaultdict(list)
        for r in rows:
            day_key = r["performed_at"].strftime("%d.%m") if r.get("performed_at") else "?"
            by_day[day_key].append(r)

        for day_key, actions in list(by_day.items())[:5]:
            ok_cnt = sum(1 for a in actions if a["success"])
            fail_cnt = len(actions) - ok_cnt
            lines.append(f"<b>📅 {day_key}</b>  ✅{ok_cnt} ❌{fail_cnt}")
            for a in actions[:8]:
                act_label = _ACTION_LABELS.get(a["action_type"], a["action_type"])
                target = html.escape(a.get("target") or "")[:40]
                if a["success"]:
                    status = "✅"
                else:
                    err = html.escape((a.get("error") or "")[:60])
                    status = f"❌ {err}"
                target_str = f" → <code>{target}</code>" if target else ""
                lines.append(f"  {status} {act_label}{target_str}")
            lines.append("")

    # Summary stats
    if rows:
        total = len(rows)
        ok_total = sum(1 for r in rows if r["success"])
        lines.append(f"<i>Показаны последние {total} действий · {ok_total} успешно</i>")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="active_plans"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
