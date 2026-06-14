"""
Account Warmup UI — управление сессиями и планами разогрева аккаунтов.

Сессия прогрева: несколько рабочих аккаунтов → конкретные цели
(каналы/боты/группы из инфраструктуры или по username/ссылке).

Режимы: Gentle (21 дн) / Standard (14 дн) / Aggressive (7 дн)
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import WarmupCb, BmCb, AccCb, ResourceActCb
from bot.states import WarmupSessionFSM, ResourceActivityFSM
from bot.utils.event_status import mark_handled_error
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_PLAN_LABELS = {
    "gentle": "🌱 Gentle (21 день, до 5 действий/день)",
    "standard": "🌿 Standard (14 дней, до 10 действий/день)",
    "aggressive": "🔥 Intensive (10 дней, до 12 действий/день)",
}


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    return kb


# ── Меню разогрева ────────────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "menu"))
async def cb_warmup_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    from services.account_warmer import get_active_plans

    plans = await get_active_plans(pool, callback.from_user.id)
    active_plans = len(plans)

    try:
        sessions = await pool.fetch(
            "SELECT COUNT(*) AS c FROM warmup_sessions WHERE owner_id=$1 AND status='active'",
            callback.from_user.id,
        )
    except Exception:
        sessions = []
    active_sessions = sessions[0]["c"] if sessions else 0

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🎯 Новая сессия прогрева", callback_data=WarmupCb(action="new_session")
    )
    kb.button(text="📋 Активные сессии", callback_data=WarmupCb(action="session_list"))
    kb.button(text="📡 Активность ресурсов", callback_data=ResourceActCb(action="menu"))
    kb.button(
        text="🔧 Одиночный план (1 аккаунт)",
        callback_data=WarmupCb(action="create_list"),
    )
    kb.button(text="📊 Активные планы", callback_data=WarmupCb(action="active_plans"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🌡 <b>Account Warming — Прогрев аккаунтов</b>\n\n"
        "Используйте аккаунты для генерации органической активности "
        "в ваших каналах, группах и ботах.\n\n"
        "<b>Новый подход — Сессия прогрева:</b>\n"
        "1️⃣ Выбираете рабочие аккаунты\n"
        "2️⃣ Выбираете цели (из инфраструктуры или по username)\n"
        "3️⃣ Выбираете режим → Старт\n\n"
        "<b>Что делает система:</b>\n"
        "📖 Читает каналы (реальный ReadHistory)\n"
        "❤️ Ставит реакции на посты\n"
        "💬 Оставляет комментарии в обсуждениях\n"
        "🤖 Взаимодействует с ботами (/start, /help)\n"
        "📌 Сохраняет посты в Saved Messages\n"
        "📊 Голосует в опросах\n\n"
        f"Активных сессий: <b>{active_sessions}</b> · Одиночных планов: <b>{active_plans}</b>\n\n"
        "<b>Режимы:</b>\n"
        "🌱 Gentle — 21 день, 5 действий/день\n"
        "🌿 Standard — 14 дней, 10 действий/день\n"
        "🔥 Aggressive — 7 дней, 20 действий/день",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Список аккаунтов для создания плана ──────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "create_list"))
async def cb_warmup_create_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    try:
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
    except Exception:
        accounts = []

    if not accounts:
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="➕ Добавить аккаунт", callback_data=AccCb(action="menu"))
        empty_kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет доступных аккаунтов</b>\n\n"
            "Добавьте Telegram-аккаунт через 📱 Аккаунты, затем вернитесь сюда.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
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

    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        acc = None
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    await callback.message.edit_text(
        f"🌡 <b>Разогрев: {html.escape(label)}</b>\n\nВыберите режим разогрева:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "select_all_plan"))
async def cb_warmup_select_all_plan(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()

    try:
        count = await pool.fetchval(
            """SELECT COUNT(*) FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND session_str IS NOT NULL AND session_str != ''""",
            callback.from_user.id,
        )
    except Exception:
        count = 0

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

    # Only accounts with a valid session can be warmed
    try:
        accounts = await pool.fetch(
            """SELECT id FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND session_str IS NOT NULL AND session_str != ''
               ORDER BY added_at DESC""",
            user_id,
        )
    except Exception:
        accounts = []

    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет аккаунтов с активной сессией</b>\n\n"
            "Для разогрева нужны аккаунты со статусом «active» и рабочей сессией.\n"
            "Аккаунты с истёкшей сессией пропускаются.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    total = len(accounts)
    msg = await callback.message.edit_text(
        f"⏳ Создаю планы разогрева для {total} аккаунтов...",
        parse_mode="HTML",
    )

    async def _create_bg():
        created = 0
        for acc in accounts:
            try:
                await create_warmup_plan(pool, user_id, acc["id"], plan_type)
                created += 1
            except Exception as exc:
                log.warning("warmup create_all: acc=%d error=%s", acc["id"], exc)
        try:
            await msg.edit_text(
                f"✅ <b>Планы разогрева созданы!</b>\n\n"
                f"Аккаунтов: <b>{created}/{total}</b>\n"
                f"Режим: <b>{_PLAN_LABELS.get(plan_type, plan_type)}</b>\n\n"
                "Разогрев запускается автоматически раз в сутки.\n"
                "Или используйте «▶️ Запустить сейчас» для немедленного старта.",
                parse_mode="HTML",
                reply_markup=_back_kb().as_markup(),
            )
        except Exception:
            log_exc_swallow(log, "warmup create_all: не удалось обновить сообщение")

    asyncio.create_task(_create_bg())


@router.callback_query(WarmupCb.filter(F.action == "start"))
async def cb_warmup_start(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    """Quick-start warmup: creates a standard plan for the given account_id and starts it.

    Used when the user taps a Start button with an explicit account_id.
    Falls back to the account picker flow if no account_id is provided.
    """
    await callback.answer()
    acc_id = callback_data.account_id

    if not acc_id:
        await cb_warmup_create_list(callback, pool)
        return

    from services.account_warmer import create_warmup_plan, run_daily_warmup, get_active_plans
    from services import task_registry

    try:
        plan_id = await create_warmup_plan(pool, callback.from_user.id, acc_id, "standard")
    except Exception as exc:
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания плана:</b> <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    plans = await get_active_plans(pool, callback.from_user.id)
    plan = next((p for p in plans if p["id"] == plan_id), None)

    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        acc = None
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    run_status = ""
    if plan:
        task = asyncio.create_task(run_daily_warmup(pool, plan))
        task_registry.register(
            callback.from_user.id, "warmup", f"Разогрев: {label}", task
        )
        run_status = "\n▶️ Первый цикл запущен в фоне."

    await callback.message.edit_text(
        f"✅ <b>План разогрева создан и запущен!</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Режим: <b>🌿 Standard (14 дней, 10 действий/день)</b>\n"
        f"ID плана: <code>{plan_id}</code>"
        f"{run_status}\n\n"
        "Следите за прогрессом в <b>⚡ Active Tasks</b>.",
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

    try:
        plan_id = await create_warmup_plan(pool, callback.from_user.id, acc_id, plan_type)
    except Exception as _e:
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания плана:</b> {html.escape(str(_e)[:200])}",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    try:
        acc = await pool.fetchrow(
            "SELECT phone, first_name FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        acc = None
    label = (acc["first_name"] or acc["phone"]) if acc else str(acc_id)

    await callback.message.edit_text(
        f"✅ <b>План разогрева создан!</b>\n\n"
        f"Аккаунт: <b>{html.escape(label)}</b>\n"
        f"Режим: <b>{_PLAN_LABELS.get(plan_type, plan_type)}</b>\n"
        f"ID плана: <code>{plan_id}</code>\n\n"
        "Разогрев запускается автоматически раз в сутки.\n"
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
        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(
            text="➕ Создать план разогрева",
            callback_data=WarmupCb(action="create_list"),
        )
        empty_kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "📋 <b>Активных планов нет</b>\n\nСоздайте план разогрева для ваших аккаунтов.",
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
        return

    lines = ["📋 <b>Активные планы разогрева</b>\n"]
    kb = InlineKeyboardBuilder()
    now_utc = datetime.now(timezone.utc)
    for plan in plans:
        label = (
            plan.get("first_name")
            or plan.get("phone")
            or str(plan.get("account_id", ""))
        )
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
            last_run_aware = (
                last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
            )
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
            callback_data=WarmupCb(
                action="plan_log", plan_id=plan["id"], account_id=plan["account_id"]
            ),
        )
        kb.button(
            text=f"▶️ Запуск {label[:10]}",
            callback_data=WarmupCb(
                action="run_one", plan_id=plan["id"], account_id=plan["account_id"]
            ),
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
    try:
        await pool.execute(
            "UPDATE account_warmup_plans SET status='cancelled' WHERE id=$1 AND owner_id=$2",
            callback_data.plan_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"warmup_delete_plan: {exc}")
        await callback.answer(f"❌ Ошибка: {str(exc)[:80]}", show_alert=True)
        return
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
            label = (
                plan.get("first_name")
                or plan.get("phone")
                or str(plan.get("account_id", ""))
            )
            try:
                await run_daily_warmup(pool, plan)
            except Exception as exc:
                log.warning("warmup run_all error acc=%s: %s", label, exc)

    task = asyncio.create_task(_run_all())
    task_registry.register(
        user_id, "warmup", f"Разогрев всех аккаунтов ({len(plans)})", task
    )

    await callback.message.edit_text(
        f"🌡 <b>Разогрев запущен в фоне</b>\n\n"
        f"Планов: <b>{len(plans)}</b>\n"
        "Процесс займёт время (20-90с между действиями).\n\n"
        "♻️ <b>Авто-возобновление:</b> при перезапуске бота разогрев "
        "продолжится автоматически в течение 1 часа.\n\n"
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
    kb.button(
        text="📋 Лог разогрева",
        callback_data=WarmupCb(action="plan_log", plan_id=plan_id, account_id=acc_id),
    )
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
    "read_channel": "📖 Читал канал",
    "join_channel": "🔔 Вступил в канал",
    "send_reaction": "❤️ Поставил реакцию",
    "search": "🔍 Поиск по слову",
    "view_profile": "👁 Смотрел профиль",
    "open_chat": "💬 Открыл чат",
    "dm_bot": "🤖 Написал боту /start",
    "mark_read": "✅ Отметил прочитанным",
    "update_presence": "🟢 Онлайн-присутствие",
    "browse_dialogs": "📱 Проверил диалоги",
    "forward_to_saved": "📌 Сохранил пост",
    "vote_poll": "📊 Проголосовал в опросе",
    "send_comment": "💬 Оставил комментарий",
    "own_channel_read": "📡 Читал свой канал",
    "smart_bot_start": "🤖 /start своему боту",
    "smart_bot_help": "🤖 /help своему боту",
    "own_bot_start": "🤖 Запустил своего бота",
    "read_messages": "📨 Читал сообщения",
}


@router.callback_query(WarmupCb.filter(F.action == "plan_log"))
async def cb_warmup_plan_log(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.account_id
    plan_id = callback_data.plan_id

    # Get account name
    try:
        acc_row = await pool.fetchrow(
            "SELECT first_name, phone FROM tg_accounts WHERE id=$1", acc_id
        )
    except Exception:
        acc_row = None
    label = ""
    if acc_row:
        label = acc_row.get("first_name") or acc_row.get("phone") or f"id{acc_id}"

    # Get plan info
    try:
        plan_row = await pool.fetchrow(
            "SELECT current_day, target_days, plan_type, daily_actions FROM account_warmup_plans WHERE id=$1",
            plan_id,
        )
    except Exception:
        plan_row = None

    # Get last 50 actions from warmup log
    try:
        rows = await pool.fetch(
            """SELECT action_type, target, success, error, performed_at
               FROM account_warmup_log
               WHERE account_id=$1
               ORDER BY performed_at DESC
               LIMIT 30""",
            acc_id,
        )
    except Exception:
        rows = []

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
            day_key = (
                r["performed_at"].strftime("%d.%m") if r.get("performed_at") else "?"
            )
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


# ══════════════════════════════════════════════════════════════════════════════
# WARMUP SESSION WIZARD — новый подход: N аккаунтов → M целей
# ══════════════════════════════════════════════════════════════════════════════

# daily — это ПОТОЛОК действий на финальных днях; реальный объём растёт по рампе
# (низкий→средний→высокий), поэтому свежий аккаунт не получает максимум сразу.
_SESSION_PLAN_CONFIG = {
    "gentle": {"days": 21, "daily": 5, "label": "🌱 Gentle (21 дн, до 5 действий/день)"},
    "standard": {
        "days": 14,
        "daily": 10,
        "label": "🌿 Standard (14 дн, до 10 действий/день)",
    },
    "aggressive": {
        "days": 10,
        "daily": 12,
        "label": "🔥 Intensive (10 дн, до 12 действий/день)",
    },
}


async def _show_account_picker(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Отрисовывает шаг 1: выбор рабочих аккаунтов с multi-select."""
    data = await state.get_data()
    selected: set[int] = set(data.get("sel_acc_ids", []))

    try:
        accounts = await pool.fetch(
            """SELECT a.id, a.phone, a.first_name,
                      COALESCE(a.acc_status, 'active') AS acc_status
               FROM tg_accounts a
               WHERE a.owner_id=$1 AND a.is_active=TRUE
                 AND a.session_str IS NOT NULL AND a.session_str != ''
               ORDER BY a.added_at DESC""",
            callback.from_user.id,
        )
    except Exception:
        accounts = []

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["id"] in selected else "⬜"
        label = acc.get("first_name") or acc["phone"]
        kb.button(
            text=f"{icon} {html.escape(label)}",
            callback_data=WarmupCb(action="tog_acc", account_id=acc["id"]),
        )
    if selected:
        kb.button(
            text=f"➡️ Готово ({len(selected)} акк.)",
            callback_data=WarmupCb(action="accs_done"),
        )
    kb.button(text="❌ Отмена", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    n_sel = len(selected)
    sel_hint = f"Выбрано: <b>{n_sel}</b>" if n_sel else "Нажмите на аккаунты для выбора"
    await callback.message.edit_text(
        "🎯 <b>Новая сессия — Шаг 1: Рабочие аккаунты</b>\n\n"
        "Выберите аккаунты, которые будут выполнять действия в целевых ресурсах.\n\n"
        f"{sel_hint}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "new_session"))
async def cb_wu_new_session(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.set_state(WarmupSessionFSM.choosing_accounts)
    await state.update_data(sel_acc_ids=[], sel_tgt_ids=[])
    await _show_account_picker(callback, state, pool)


@router.callback_query(
    WarmupCb.filter(F.action == "tog_acc"), WarmupSessionFSM.choosing_accounts
)
async def cb_wu_toggle_acc(
    callback: CallbackQuery,
    callback_data: WarmupCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = data.get("sel_acc_ids", [])
    acc_id = callback_data.account_id
    if acc_id in selected:
        selected = [x for x in selected if x != acc_id]
    else:
        selected = selected + [acc_id]
    await state.update_data(sel_acc_ids=selected)
    await _show_account_picker(callback, state, pool)


@router.callback_query(
    WarmupCb.filter(F.action == "accs_done"), WarmupSessionFSM.choosing_accounts
)
async def cb_wu_accs_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("sel_acc_ids"):
        await callback.answer("Выберите хотя бы один аккаунт", show_alert=True)
        return
    await callback.answer()
    await state.set_state(WarmupSessionFSM.choosing_target_type)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🏗️ Из моей инфраструктуры", callback_data=WarmupCb(action="tgt_infra")
    )
    kb.button(text="📝 По username/ссылке", callback_data=WarmupCb(action="tgt_manual"))
    kb.button(text="📋 Списком (несколько)", callback_data=WarmupCb(action="tgt_list"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="new_session"))
    kb.adjust(1)

    n = len(data["sel_acc_ids"])
    await callback.message.edit_text(
        f"🎯 <b>Новая сессия — Шаг 2: Цели прогрева</b>\n\n"
        f"Выбрано аккаунтов: <b>{n}</b>\n\n"
        "Откуда брать цели для прогрева?\n\n"
        "🏗️ <b>Из инфраструктуры</b> — выберите ваши каналы/боты/группы\n"
        "📝 <b>По username</b> — введите один @username или invite link\n"
        "📋 <b>Списком</b> — введите несколько целей (по одной на строку)",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _show_infra_picker(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Отрисовывает шаг 2b: выбор целей из собственной инфраструктуры."""
    data = await state.get_data()
    selected_refs: list[str] = data.get("sel_tgt_refs", [])
    selected_set: set[str] = set(selected_refs)

    try:
        channels = await pool.fetch(
            """SELECT DISTINCT channel_id::text AS ref,
                      COALESCE(username, title, 'id'||channel_id::text) AS label,
                      'ch' AS kind
               FROM managed_channels
               WHERE owner_id=$1
               LIMIT 15""",
            callback.from_user.id,
        )
    except Exception:
        channels = []
    try:
        bots = await pool.fetch(
            """SELECT DISTINCT username AS ref,
                      COALESCE(first_name, username) AS label,
                      'bt' AS kind
               FROM managed_bots
               WHERE added_by=$1 AND is_active=TRUE AND username IS NOT NULL AND username != ''
               LIMIT 10""",
            callback.from_user.id,
        )
    except Exception:
        bots = []

    all_resources = list(channels) + list(bots)

    # Store resource mapping in FSM for toggle resolution
    res_map = {str(hash(r["ref"]) & 0x7FFFFFFF): r["ref"] for r in all_resources}
    await state.update_data(_infra_map=res_map)

    kb = InlineKeyboardBuilder()
    for r in all_resources:
        ref = r["ref"]
        icon = "✅" if ref in selected_set else "⬜"
        kind_icon = "📡" if r["kind"] == "ch" else "🤖"
        kb.button(
            text=f"{icon} {kind_icon} {html.escape(str(r['label'])[:30])}",
            callback_data=WarmupCb(action="tog_tgt", account_id=hash(ref) & 0x7FFFFFFF),
        )
    if selected_refs:
        kb.button(
            text=f"➡️ Готово ({len(selected_refs)} целей)",
            callback_data=WarmupCb(action="tgts_done"),
        )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="back_to_targets"))
    kb.button(text="❌ Отмена", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    n_sel = len(selected_refs)
    sel_hint = f"Выбрано: <b>{n_sel}</b>" if n_sel else "Нажмите для выбора"
    await callback.message.edit_text(
        "🎯 <b>Новая сессия — Шаг 2: Выбор целей из инфраструктуры</b>\n\n"
        "📡 = канал/группа · 🤖 = бот\n\n"
        f"{sel_hint}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(F.action == "tgt_infra"),
    WarmupSessionFSM.choosing_target_type,
)
async def cb_wu_tgt_infra(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.set_state(WarmupSessionFSM.picking_infra)
    await state.update_data(sel_tgt_refs=[], target_type="infra")
    await _show_infra_picker(callback, state, pool)


@router.callback_query(
    WarmupCb.filter(F.action == "tog_tgt"), WarmupSessionFSM.picking_infra
)
async def cb_wu_toggle_tgt(
    callback: CallbackQuery,
    callback_data: WarmupCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    res_map: dict = data.get("_infra_map", {})
    ref = res_map.get(str(callback_data.account_id), "")
    if not ref:
        return
    refs: list[str] = data.get("sel_tgt_refs", [])
    if ref in refs:
        refs = [x for x in refs if x != ref]
    else:
        refs = refs + [ref]
    await state.update_data(sel_tgt_refs=refs)
    await _show_infra_picker(callback, state, pool)


@router.callback_query(
    WarmupCb.filter(F.action == "tgts_done"), WarmupSessionFSM.picking_infra
)
async def cb_wu_infra_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("sel_tgt_refs"):
        await callback.answer("Выберите хотя бы одну цель", show_alert=True)
        return
    await callback.answer()
    await _show_mode_picker(callback, state)


@router.callback_query(WarmupCb.filter(F.action == "back_to_targets"))
async def cb_wu_back_to_targets(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """Back button from infra picker or mode picker — returns to target type chooser.

    Works from any FSM state (picking_infra, choosing_mode, confirming).
    Restores choosing_target_type without losing account selection.
    """
    await callback.answer()
    await state.set_state(WarmupSessionFSM.choosing_target_type)
    data = await state.get_data()
    n = len(data.get("sel_acc_ids", []))

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🏗️ Из моей инфраструктуры", callback_data=WarmupCb(action="tgt_infra")
    )
    kb.button(text="📝 По username/ссылке", callback_data=WarmupCb(action="tgt_manual"))
    kb.button(text="📋 Списком (несколько)", callback_data=WarmupCb(action="tgt_list"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="new_session"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"🎯 <b>Новая сессия — Шаг 2: Цели прогрева</b>\n\n"
        f"Выбрано аккаунтов: <b>{n}</b>\n\n"
        "Откуда брать цели для прогрева?\n\n"
        "🏗️ <b>Из инфраструктуры</b> — выберите ваши каналы/боты/группы\n"
        "📝 <b>По username</b> — введите один @username или invite link\n"
        "📋 <b>Списком</b> — введите несколько целей (по одной на строку)",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(F.action.in_({"tgt_manual", "tgt_list"})),
    WarmupSessionFSM.choosing_target_type,
)
async def cb_wu_tgt_manual(
    callback: CallbackQuery, callback_data: WarmupCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(WarmupSessionFSM.entering_targets)
    is_list = callback_data.action == "tgt_list"
    await state.update_data(target_type="manual", sel_tgt_refs=[])

    hint = (
        "Введите несколько целей, каждую с новой строки:\n\n"
        "<code>@channel_name\n@bot_username\nhttps://t.me/+invite</code>"
        if is_list
        else "Введите username или invite link:\n\n<code>@channel_name</code>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"🎯 <b>Новая сессия — Шаг 2: Цели прогрева</b>\n\n{hint}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(WarmupSessionFSM.entering_targets)
async def fsm_wu_targets_text(message: Message, state: FSMContext) -> None:
    import re

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("⚠️ Введите хотя бы один username или ссылку.")
        return

    parts = re.split(r"[\s,;]+", raw)
    refs = [
        p.strip()
        for p in parts
        if p.strip()
        and (p.startswith("@") or p.startswith("http") or p.startswith("+"))
    ]
    if not refs:
        refs = [p.strip() for p in parts if p.strip()]

    await state.update_data(sel_tgt_refs=refs)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=WarmupCb(action="tgts_text_done"))
    kb.button(text="❌ Отмена", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    refs_preview = "\n".join(f"  • <code>{html.escape(r)}</code>" for r in refs[:10])
    if len(refs) > 10:
        refs_preview += f"\n  <i>...и ещё {len(refs) - 10}</i>"

    await message.answer(
        f"📋 <b>Найдено целей: {len(refs)}</b>\n\n{refs_preview}\n\nПодтвердить?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "tgts_text_done"))
async def cb_wu_tgts_text_done(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_mode_picker(callback, state)


async def _show_mode_picker(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WarmupSessionFSM.choosing_mode)
    kb = InlineKeyboardBuilder()
    for key, cfg in _SESSION_PLAN_CONFIG.items():
        kb.button(text=cfg["label"], callback_data=WarmupCb(action=f"sess_mode_{key}"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="back_to_targets"))
    kb.adjust(1)

    data = await state.get_data()
    n_acc = len(data.get("sel_acc_ids", []))
    n_tgt = len(data.get("sel_tgt_refs", []))

    await callback.message.edit_text(
        f"🎯 <b>Новая сессия — Шаг 3: Режим прогрева</b>\n\n"
        f"Аккаунтов: <b>{n_acc}</b> · Целей: <b>{n_tgt}</b>\n\n"
        "Выберите интенсивность прогрева:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(
        F.action.in_({"sess_mode_gentle", "sess_mode_standard", "sess_mode_aggressive"})
    ),
    WarmupSessionFSM.choosing_mode,
)
async def cb_wu_mode(
    callback: CallbackQuery,
    callback_data: WarmupCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    plan_type = callback_data.action.replace("sess_mode_", "")
    await state.update_data(plan_type=plan_type)
    await state.set_state(WarmupSessionFSM.confirming)

    data = await state.get_data()
    acc_ids: list[int] = data.get("sel_acc_ids", [])
    tgt_refs: list[str] = data.get("sel_tgt_refs", [])
    target_type: str = data.get("target_type", "infra")
    cfg = _SESSION_PLAN_CONFIG[plan_type]

    try:
        acc_rows = await pool.fetch(
            "SELECT id, COALESCE(first_name, phone) AS label FROM tg_accounts WHERE id=ANY($1)",
            acc_ids,
        )
    except Exception:
        acc_rows = []
    acc_labels = [html.escape(str(r["label"])) for r in acc_rows]

    tgt_preview = "\n".join(f"  • <code>{html.escape(r)}</code>" for r in tgt_refs[:5])
    if len(tgt_refs) > 5:
        tgt_preview += f"\n  <i>...и ещё {len(tgt_refs) - 5}</i>"
    acc_preview = ", ".join(acc_labels[:5])
    if len(acc_labels) > 5:
        acc_preview += f" и ещё {len(acc_labels) - 5}"

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Запустить сессию", callback_data=WarmupCb(action="sess_start"))
    kb.button(text="◀️ Изменить режим", callback_data=WarmupCb(action="back_to_targets"))
    kb.button(text="❌ Отмена", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    tgt_type_label = "Из инфраструктуры" if target_type == "infra" else "Вручную"
    await callback.message.edit_text(
        f"🎯 <b>Новая сессия — Подтверждение</b>\n\n"
        f"<b>Аккаунты ({len(acc_ids)}):</b> {acc_preview}\n\n"
        f"<b>Цели ({len(tgt_refs)}) [{tgt_type_label}]:</b>\n{tgt_preview}\n\n"
        f"<b>Режим:</b> {cfg['label']}\n"
        f"<b>Длительность:</b> {cfg['days']} дней · {cfg['daily']} действий/день\n\n"
        "Всё верно? Нажмите ▶️ для запуска.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    WarmupCb.filter(F.action == "sess_start"), WarmupSessionFSM.confirming
)
async def cb_wu_sess_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю сессию...")
    data = await state.get_data()
    acc_ids: list[int] = data.get("sel_acc_ids", [])
    tgt_refs: list[str] = data.get("sel_tgt_refs", [])
    target_type: str = data.get("target_type", "infra")
    plan_type: str = data.get("plan_type", "standard")
    cfg = _SESSION_PLAN_CONFIG[plan_type]

    try:
        session_id = await pool.fetchval(
            """INSERT INTO warmup_sessions
               (owner_id, account_ids, target_type, target_refs, plan_type, target_days, daily_actions)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id""",
            callback.from_user.id,
            acc_ids,
            target_type,
            tgt_refs,
            plan_type,
            cfg["days"],
            cfg["daily"],
        )
    except Exception as exc:
        mark_handled_error(f"wu_sess_start insert: {exc}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания сессии:</b> <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все сессии", callback_data=WarmupCb(action="session_list"))
    kb.button(
        text="▶️ Запустить сейчас",
        callback_data=WarmupCb(action="sess_run", session_id=session_id),
    )
    kb.button(text="◀️ В меню прогрева", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Сессия прогрева создана!</b>\n\n"
        f"ID: <code>{session_id}</code>\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Целей: <b>{len(tgt_refs)}</b>\n"
        f"Режим: <b>{cfg['label']}</b>\n\n"
        "Прогрев запускается автоматически каждые 24ч.\n"
        "Или нажмите ▶️ для немедленного запуска.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Список активных сессий ────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "session_list"))
async def cb_wu_session_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    try:
        sessions = await pool.fetch(
            """SELECT id, account_ids, target_refs, plan_type, status,
                      current_day, target_days, daily_actions, last_run_at, created_at
               FROM warmup_sessions
               WHERE owner_id=$1 AND status IN ('active', 'paused')
               ORDER BY created_at DESC
               LIMIT 20""",
            callback.from_user.id,
        )
    except Exception:
        sessions = []

    if not sessions:
        kb = InlineKeyboardBuilder()
        kb.button(text="🎯 Новая сессия", callback_data=WarmupCb(action="new_session"))
        kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "📋 <b>Активных сессий нет</b>\n\nСоздайте новую сессию прогрева.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📋 <b>Активные сессии прогрева</b>\n"]
    kb = InlineKeyboardBuilder()
    now_utc = datetime.now(timezone.utc)

    for s in sessions:
        n_acc = len(s["account_ids"] or [])
        n_tgt = len(s["target_refs"] or [])
        day = s["current_day"] or 0
        days = max(s["target_days"] or 1, 1)
        pct = round(day / days * 100)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        status_icon = "🟢" if s["status"] == "active" else "⏸"

        last_run = s["last_run_at"]
        if last_run:
            last_run_aware = (
                last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
            )
            next_run = last_run_aware + timedelta(hours=24)
            if next_run > now_utc:
                diff = next_run - now_utc
                h = int(diff.total_seconds() // 3600)
                m = int((diff.total_seconds() % 3600) // 60)
                timing = f"⏰ через {h}ч {m}м"
            else:
                timing = "⏰ готова к запуску"
        else:
            timing = "⏰ не запускалась"

        lines.append(
            f"{status_icon} <b>Сессия #{s['id']}</b>\n"
            f"  [{bar}] День {day}/{days} ({pct}%)\n"
            f"  {n_acc} акк. → {n_tgt} цел. · {s['plan_type']} · {s['daily_actions']}/день\n"
            f"  {timing}"
        )
        kb.button(
            text=f"📊 #{s['id']} {s['plan_type']}",
            callback_data=WarmupCb(action="sess_detail", session_id=s["id"]),
        )

    kb.button(text="🎯 Новая сессия", callback_data=WarmupCb(action="new_session"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Детали сессии ─────────────────────────────────────────────────────────


@router.callback_query(WarmupCb.filter(F.action == "sess_detail"))
async def cb_wu_sess_detail(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    sess_id = callback_data.session_id

    try:
        s = await pool.fetchrow(
            "SELECT * FROM warmup_sessions WHERE id=$1 AND owner_id=$2",
            sess_id,
            callback.from_user.id,
        )
    except Exception:
        s = None
    if not s:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    await callback.answer()

    try:
        logs = await pool.fetch(
            """SELECT account_id, action_type, target, success, performed_at
               FROM warmup_session_log
               WHERE session_id=$1
               ORDER BY performed_at DESC
               LIMIT 20""",
            sess_id,
        )
    except Exception:
        logs = []

    n_acc = len(s["account_ids"] or [])
    n_tgt = len(s["target_refs"] or [])
    day = s["current_day"] or 0
    days = s["target_days"] or 1
    ok_count = sum(1 for l in logs if l["success"])
    fail_count = len(logs) - ok_count

    lines = [
        f"📊 <b>Сессия #{sess_id}</b>\n",
        f"Статус: <b>{s['status']}</b> · Режим: <b>{s['plan_type']}</b>",
        f"День: <b>{day}/{days}</b> · {n_acc} акк. → {n_tgt} цел.",
        f"Действий/день: <b>{s['daily_actions']}</b>\n",
    ]

    if logs:
        lines.append(f"<b>Последние действия</b> (✅{ok_count} ❌{fail_count}):")
        for l in logs[:10]:
            act_label = _ACTION_LABELS.get(l["action_type"], l["action_type"])
            target = html.escape((l["target"] or "")[:35])
            status = "✅" if l["success"] else "❌"
            target_str = f" → <code>{target}</code>" if target else ""
            lines.append(f"  {status} {act_label}{target_str}")
    else:
        lines.append("<i>Действий ещё не выполнено.</i>")

    kb = InlineKeyboardBuilder()
    if s["status"] == "active":
        kb.button(
            text="▶️ Запустить сейчас",
            callback_data=WarmupCb(action="sess_run", session_id=sess_id),
        )
        kb.button(
            text="⏸ Пауза",
            callback_data=WarmupCb(action="sess_pause", session_id=sess_id),
        )
    else:
        kb.button(
            text="▶️ Возобновить",
            callback_data=WarmupCb(action="sess_resume", session_id=sess_id),
        )
    kb.button(
        text="🗑 Удалить",
        callback_data=WarmupCb(action="sess_delete", session_id=sess_id),
    )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="session_list"))
    kb.adjust(2)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(WarmupCb.filter(F.action == "sess_run"))
async def cb_wu_sess_run(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    from services.account_warmer import run_warmup_session
    from services import task_registry
    import asyncio

    sess_id = callback_data.session_id
    try:
        s = await pool.fetchrow(
            "SELECT * FROM warmup_sessions WHERE id=$1 AND owner_id=$2 AND status='active'",
            sess_id,
            callback.from_user.id,
        )
    except Exception:
        s = None
    if not s:
        await callback.answer("Сессия не найдена или не активна", show_alert=True)
        return
    await callback.answer("▶️ Запускаю в фоне...")

    task = asyncio.create_task(run_warmup_session(pool, dict(s)))
    task_registry.register(
        callback.from_user.id,
        "warmup_session",
        f"Прогрев сессии #{sess_id} ({s['plan_type']})",
        task,
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Детали сессии",
        callback_data=WarmupCb(action="sess_detail", session_id=sess_id),
    )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="session_list"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"▶️ <b>Сессия #{sess_id} запущена в фоне</b>\n\n"
        "Следите за прогрессом в <b>⚡ Active Tasks</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(WarmupCb.filter(F.action == "sess_pause"))
async def cb_wu_sess_pause(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    sess_id = callback_data.session_id
    try:
        await pool.execute(
            "UPDATE warmup_sessions SET status='paused' WHERE id=$1 AND owner_id=$2",
            sess_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"wu_sess_pause: {exc}")
        await callback.answer(f"❌ Ошибка: {str(exc)[:80]}", show_alert=True)
        return
    await callback.answer("⏸ Сессия поставлена на паузу", show_alert=True)


@router.callback_query(WarmupCb.filter(F.action == "sess_resume"))
async def cb_wu_sess_resume(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    sess_id = callback_data.session_id
    try:
        await pool.execute(
            "UPDATE warmup_sessions SET status='active' WHERE id=$1 AND owner_id=$2",
            sess_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"wu_sess_resume: {exc}")
        await callback.answer(f"❌ Ошибка: {str(exc)[:80]}", show_alert=True)
        return
    await callback.answer("▶️ Сессия возобновлена", show_alert=True)


@router.callback_query(WarmupCb.filter(F.action == "sess_delete"))
async def cb_wu_sess_delete(
    callback: CallbackQuery, callback_data: WarmupCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    sess_id = callback_data.session_id
    try:
        await pool.execute(
            "DELETE FROM warmup_sessions WHERE id=$1 AND owner_id=$2",
            sess_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"wu_sess_delete: {exc}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка удаления:</b> <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    await callback.message.edit_text(
        "🗑 <b>Сессия удалена</b>",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# RESOURCE ACTIVITY ENGINE — активность в собственных ресурсах
# Профили: reader | commenter | reactor | mixed
# Адаптивный пейсинг при FloodWait
# ══════════════════════════════════════════════════════════════════════════════

_RACT_PROFILE_LABELS = {
    "reader": "📖 Reader — чтение и просмотр (низкий риск)",
    "commenter": "💬 Commenter — акцент на комментарии",
    "reactor": "❤️ Reactor — акцент на реакции",
    "mixed": "🔀 Mixed — все типы действий",
}

_RACT_CONFIG = {
    "short": {"days": 7, "daily": 6, "label": "⚡ Короткий (7 дней, 6 действий/день)"},
    "standard": {
        "days": 14,
        "daily": 8,
        "label": "🌿 Стандарт (14 дней, 8 действий/день)",
    },
    "long": {"days": 30, "daily": 5, "label": "🌱 Долгий (30 дней, 5 действий/день)"},
}


def _ract_back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ResourceActCb(action="menu"))
    return kb


@router.callback_query(ResourceActCb.filter(F.action == "menu"))
async def cb_ract_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    uid = callback.from_user.id
    try:
        active_count = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM resource_activity_sessions WHERE owner_id=$1 AND status='active'",
                uid,
            )
            or 0
        )
    except Exception:
        active_count = 0

    try:
        resources = (
            await pool.fetchval(
                """SELECT COUNT(*) FROM (
               SELECT DISTINCT channel_id FROM managed_channels WHERE owner_id=$1
               UNION ALL
               SELECT DISTINCT bot_id FROM managed_bots WHERE added_by=$1 AND is_active=TRUE
               ) x""",
                uid,
            )
            or 0
        )
    except Exception:
        resources = 0

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Новая сессия активности", callback_data=ResourceActCb(action="new")
    )
    kb.button(text="📋 Мои сессии", callback_data=ResourceActCb(action="list"))
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "📡 <b>Активность ресурсов</b>\n\n"
        "Создаёт органическую активность в ваших каналах, ботах и группах. "
        "Несколько аккаунтов читают, реагируют и комментируют посты в ваших ресурсах — "
        "имитируя реального пользователя.\n\n"
        "<b>Профили:</b>\n"
        "📖 Reader — читает посты, отмечает прочитанным\n"
        "❤️ Reactor — ставит реакции на посты\n"
        "💬 Commenter — оставляет комментарии (нужна discussion group)\n"
        "🔀 Mixed — все типы действий\n\n"
        f"Ресурсов в инфраструктуре: <b>{resources}</b>\n"
        f"Активных сессий: <b>{active_count}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _show_ract_account_picker(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    selected: set[int] = set(data.get("ract_acc_ids", []))

    try:
        accounts = await pool.fetch(
            """SELECT a.id, a.phone, a.first_name,
                      COALESCE(a.acc_status, 'active') AS acc_status
               FROM tg_accounts a
               WHERE a.owner_id=$1 AND a.is_active=TRUE
                 AND a.session_str IS NOT NULL AND a.session_str != ''
               ORDER BY a.added_at DESC""",
            callback.from_user.id,
        )
    except Exception:
        accounts = []

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["id"] in selected else "⬜"
        label = acc.get("first_name") or acc["phone"]
        kb.button(
            text=f"{icon} {html.escape(label)}",
            callback_data=ResourceActCb(action="tog_acc", account_id=acc["id"]),
        )
    if selected:
        kb.button(
            text=f"➡️ Готово ({len(selected)} акк.)",
            callback_data=ResourceActCb(action="accs_done"),
        )
    kb.button(text="❌ Отмена", callback_data=ResourceActCb(action="menu"))
    kb.adjust(1)

    n_sel = len(selected)
    hint = f"Выбрано: <b>{n_sel}</b>" if n_sel else "Нажмите для выбора"
    await callback.message.edit_text(
        "📡 <b>Активность ресурсов — Шаг 1: Аккаунты</b>\n\n"
        "Выберите аккаунты, которые будут создавать активность в ваших ресурсах.\n\n"
        f"{hint}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ResourceActCb.filter(F.action == "new"))
async def cb_ract_new(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.set_state(ResourceActivityFSM.choosing_accounts)
    await state.update_data(ract_acc_ids=[])
    await _show_ract_account_picker(callback, state, pool)


@router.callback_query(
    ResourceActCb.filter(F.action == "tog_acc"), ResourceActivityFSM.choosing_accounts
)
async def cb_ract_toggle_acc(
    callback: CallbackQuery,
    callback_data: ResourceActCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected: list[int] = data.get("ract_acc_ids", [])
    acc_id = callback_data.account_id
    if acc_id in selected:
        selected = [x for x in selected if x != acc_id]
    else:
        selected = selected + [acc_id]
    await state.update_data(ract_acc_ids=selected)
    await _show_ract_account_picker(callback, state, pool)


@router.callback_query(
    ResourceActCb.filter(F.action == "accs_done"),
    StateFilter(ResourceActivityFSM.choosing_accounts, ResourceActivityFSM.confirming),
)
async def cb_ract_accs_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("ract_acc_ids"):
        await callback.answer("Выберите хотя бы один аккаунт", show_alert=True)
        return
    await callback.answer()
    await state.set_state(ResourceActivityFSM.choosing_profile)

    kb = InlineKeyboardBuilder()
    for profile_key, profile_label in _RACT_PROFILE_LABELS.items():
        kb.button(
            text=profile_label,
            callback_data=ResourceActCb(action=f"profile_{profile_key}"),
        )
    kb.button(text="❌ Отмена", callback_data=ResourceActCb(action="menu"))
    kb.adjust(1)

    n = len(data["ract_acc_ids"])
    await callback.message.edit_text(
        f"📡 <b>Активность ресурсов — Шаг 2: Профиль</b>\n\n"
        f"Аккаунтов: <b>{n}</b>\n\n"
        "Выберите профиль активности:\n\n"
        "📖 <b>Reader</b> — безопасно, только чтение/просмотр\n"
        "❤️ <b>Reactor</b> — реакции на посты\n"
        "💬 <b>Commenter</b> — комментарии (нужна linked discussion group)\n"
        "🔀 <b>Mixed</b> — все виды активности",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    ResourceActCb.filter(
        F.action.in_(
            {"profile_reader", "profile_commenter", "profile_reactor", "profile_mixed"}
        )
    ),
    ResourceActivityFSM.choosing_profile,
)
async def cb_ract_profile(
    callback: CallbackQuery,
    callback_data: ResourceActCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    profile = callback_data.action.replace("profile_", "")
    await state.update_data(ract_profile=profile)
    await state.set_state(ResourceActivityFSM.confirming)

    data = await state.get_data()
    acc_ids: list[int] = data.get("ract_acc_ids", [])

    # Preview own resources
    try:
        resources = await pool.fetch(
            """SELECT COALESCE('@'||username, title) AS label
               FROM managed_channels WHERE owner_id=$1 LIMIT 5""",
            callback.from_user.id,
        )
    except Exception:
        resources = []
    res_preview = "\n".join(
        f"  • <code>{html.escape(str(r['label']))}</code>" for r in resources
    )
    if not res_preview:
        res_preview = "  <i>Ресурсы из инфраструктуры (авто)</i>"

    try:
        acc_rows = await pool.fetch(
            "SELECT id, COALESCE(first_name, phone) AS label FROM tg_accounts WHERE id=ANY($1)",
            acc_ids,
        )
    except Exception:
        acc_rows = []
    acc_preview = ", ".join(html.escape(str(r["label"])) for r in acc_rows[:4])
    if len(acc_rows) > 4:
        acc_preview += f" и ещё {len(acc_rows) - 4}"

    profile_label = _RACT_PROFILE_LABELS.get(profile, profile)
    cfg = _RACT_CONFIG["standard"]

    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Запустить", callback_data=ResourceActCb(action="start"))
    kb.button(
        text="◀️ Изменить профиль", callback_data=ResourceActCb(action="accs_done")
    )
    kb.button(text="❌ Отмена", callback_data=ResourceActCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"📡 <b>Активность ресурсов — Подтверждение</b>\n\n"
        f"<b>Аккаунты ({len(acc_ids)}):</b> {acc_preview}\n\n"
        f"<b>Ресурсы (авто из инфраструктуры):</b>\n{res_preview}\n\n"
        f"<b>Профиль:</b> {profile_label}\n"
        f"<b>Длительность:</b> {cfg['days']} дней · {cfg['daily']} действий/день\n\n"
        "Ресурсы определяются автоматически из ваших каналов и ботов. "
        "Нажмите ▶️ для запуска.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    ResourceActCb.filter(F.action == "start"), ResourceActivityFSM.confirming
)
async def cb_ract_start(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю сессию...")
    data = await state.get_data()
    acc_ids: list[int] = data.get("ract_acc_ids", [])
    profile: str = data.get("ract_profile", "mixed")
    cfg = _RACT_CONFIG["standard"]

    try:
        sess_id = await pool.fetchval(
            """INSERT INTO resource_activity_sessions
               (owner_id, account_ids, resource_refs, profile_type, target_days, daily_actions)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id""",
            callback.from_user.id,
            acc_ids,
            [],
            profile,
            cfg["days"],
            cfg["daily"],
        )
    except Exception as exc:
        mark_handled_error(f"ract_start insert: {exc}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания сессии:</b> <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
        )
        return

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои сессии", callback_data=ResourceActCb(action="list"))
    kb.button(
        text="▶️ Запустить сейчас",
        callback_data=ResourceActCb(action="run", session_id=sess_id),
    )
    kb.button(text="◀️ В меню", callback_data=ResourceActCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Сессия активности создана!</b>\n\n"
        f"ID: <code>{sess_id}</code>\n"
        f"Аккаунтов: <b>{len(acc_ids)}</b>\n"
        f"Профиль: <b>{_RACT_PROFILE_LABELS.get(profile, profile)}</b>\n"
        f"Длительность: <b>{cfg['days']}</b> дней · <b>{cfg['daily']}</b> действий/день\n\n"
        "Активность запускается автоматически каждые 24ч. "
        "Или нажмите ▶️ для немедленного запуска.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ResourceActCb.filter(F.action == "list"))
async def cb_ract_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    try:
        sessions = await pool.fetch(
            """SELECT id, account_ids, profile_type, status, current_day, target_days,
                      daily_actions, last_run_at, created_at
               FROM resource_activity_sessions
               WHERE owner_id=$1 AND status IN ('active', 'paused')
               ORDER BY created_at DESC LIMIT 15""",
            uid,
        )
    except Exception:
        sessions = []

    if not sessions:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Новая сессия", callback_data=ResourceActCb(action="new"))
        kb.button(text="◀️ Назад", callback_data=ResourceActCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "📋 <b>Активных сессий нет</b>\n\nСоздайте сессию активности для ваших ресурсов.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📋 <b>Сессии активности ресурсов</b>\n"]
    kb = InlineKeyboardBuilder()
    now_utc = datetime.now(timezone.utc)

    for s in sessions:
        n_acc = len(s["account_ids"] or [])
        day = s["current_day"] or 0
        days = max(s["target_days"] or 1, 1)
        pct = round(day / days * 100)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        icon = "🟢" if s["status"] == "active" else "⏸"
        profile_label = _RACT_PROFILE_LABELS.get(
            s["profile_type"], s["profile_type"]
        ).split(" — ")[0]

        last = s["last_run_at"]
        if last:
            last_aware = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            next_run = last_aware + timedelta(hours=24)
            diff = next_run - now_utc
            if diff.total_seconds() > 0:
                h = int(diff.total_seconds() // 3600)
                m = int((diff.total_seconds() % 3600) // 60)
                timing = f"⏰ через {h}ч {m}м"
            else:
                timing = "⏰ готова к запуску"
        else:
            timing = "⏰ не запускалась"

        lines.append(
            f"{icon} <b>Сессия #{s['id']}</b> · {profile_label}\n"
            f"  [{bar}] День {day}/{days} ({pct}%) · {n_acc} акк.\n"
            f"  {timing}"
        )
        kb.button(
            text=f"📊 #{s['id']} {s['profile_type']}",
            callback_data=ResourceActCb(action="detail", session_id=s["id"]),
        )

    kb.button(text="➕ Новая сессия", callback_data=ResourceActCb(action="new"))
    kb.button(text="◀️ Назад", callback_data=ResourceActCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ResourceActCb.filter(F.action == "detail"))
async def cb_ract_detail(
    callback: CallbackQuery, callback_data: ResourceActCb, pool: asyncpg.Pool
) -> None:
    sess_id = callback_data.session_id
    uid = callback.from_user.id

    try:
        s = await pool.fetchrow(
            "SELECT * FROM resource_activity_sessions WHERE id=$1 AND owner_id=$2",
            sess_id,
            uid,
        )
    except Exception:
        s = None
    if not s:
        await callback.answer("Сессия не найдена", show_alert=True)
        return
    await callback.answer()

    try:
        logs = await pool.fetch(
            """SELECT account_id, action_type, resource_ref, success, performed_at
               FROM resource_activity_log
               WHERE session_id=$1
               ORDER BY performed_at DESC LIMIT 20""",
            sess_id,
        )
    except Exception:
        logs = []

    day = s["current_day"] or 0
    days = s["target_days"] or 1
    ok = sum(1 for l in logs if l["success"])
    fail = len(logs) - ok
    profile_label = _RACT_PROFILE_LABELS.get(s["profile_type"], s["profile_type"])

    lines = [
        f"📡 <b>Сессия активности #{sess_id}</b>\n",
        f"Профиль: <b>{profile_label}</b>",
        f"Статус: <b>{s['status']}</b> · День: <b>{day}/{days}</b>",
        f"Аккаунтов: <b>{len(s['account_ids'] or [])}</b> · Действий/день: <b>{s['daily_actions']}</b>\n",
    ]

    if logs:
        from services.activity_engine import _ACTION_LABELS as _AELABELS

        lines.append(f"<b>Последние действия</b> (✅{ok} ❌{fail}):")
        for l in logs[:10]:
            act_label = _AELABELS.get(l["action_type"], l["action_type"])
            ref = html.escape((l["resource_ref"] or "")[:35])
            status = "✅" if l["success"] else "❌"
            ref_str = f" → <code>{ref}</code>" if ref else ""
            lines.append(f"  {status} {act_label}{ref_str}")
    else:
        lines.append("<i>Действий ещё не выполнено.</i>")

    kb = InlineKeyboardBuilder()
    if s["status"] == "active":
        kb.button(
            text="▶️ Запустить сейчас",
            callback_data=ResourceActCb(action="run", session_id=sess_id),
        )
        kb.button(
            text="⏸ Пауза",
            callback_data=ResourceActCb(action="pause", session_id=sess_id),
        )
    else:
        kb.button(
            text="▶️ Возобновить",
            callback_data=ResourceActCb(action="resume", session_id=sess_id),
        )
    kb.button(
        text="🗑 Удалить",
        callback_data=ResourceActCb(action="delete", session_id=sess_id),
    )
    kb.button(text="◀️ Назад", callback_data=ResourceActCb(action="list"))
    kb.adjust(2)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(ResourceActCb.filter(F.action == "run"))
async def cb_ract_run(
    callback: CallbackQuery, callback_data: ResourceActCb, pool: asyncpg.Pool
) -> None:
    from services.activity_engine import run_resource_activity_session
    from services import task_registry
    import asyncio

    sess_id = callback_data.session_id
    uid = callback.from_user.id
    try:
        s = await pool.fetchrow(
            "SELECT * FROM resource_activity_sessions WHERE id=$1 AND owner_id=$2 AND status='active'",
            sess_id,
            uid,
        )
    except Exception:
        s = None
    if not s:
        await callback.answer("Сессия не найдена или не активна", show_alert=True)
        return
    await callback.answer("▶️ Запускаю в фоне...")

    task = asyncio.create_task(run_resource_activity_session(pool, dict(s)))
    task_registry.register(
        uid,
        "resource_activity",
        f"Активность ресурсов #{sess_id} ({s['profile_type']})",
        task,
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Детали",
        callback_data=ResourceActCb(action="detail", session_id=sess_id),
    )
    kb.button(text="◀️ Назад", callback_data=ResourceActCb(action="list"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"▶️ <b>Сессия #{sess_id} запущена в фоне</b>\n\n"
        "Следите за прогрессом в <b>⚡ Active Tasks</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ResourceActCb.filter(F.action == "pause"))
async def cb_ract_pause(
    callback: CallbackQuery, callback_data: ResourceActCb, pool: asyncpg.Pool
) -> None:
    try:
        await pool.execute(
            "UPDATE resource_activity_sessions SET status='paused' WHERE id=$1 AND owner_id=$2",
            callback_data.session_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"ract_pause: {exc}")
        await callback.answer(f"❌ Ошибка: {str(exc)[:80]}", show_alert=True)
        return
    await callback.answer("⏸ Сессия поставлена на паузу", show_alert=True)


@router.callback_query(ResourceActCb.filter(F.action == "resume"))
async def cb_ract_resume(
    callback: CallbackQuery, callback_data: ResourceActCb, pool: asyncpg.Pool
) -> None:
    try:
        await pool.execute(
            "UPDATE resource_activity_sessions SET status='active' WHERE id=$1 AND owner_id=$2",
            callback_data.session_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"ract_resume: {exc}")
        await callback.answer(f"❌ Ошибка: {str(exc)[:80]}", show_alert=True)
        return
    await callback.answer("▶️ Сессия возобновлена", show_alert=True)


@router.callback_query(ResourceActCb.filter(F.action == "delete"))
async def cb_ract_delete(
    callback: CallbackQuery, callback_data: ResourceActCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    try:
        await pool.execute(
            "DELETE FROM resource_activity_sessions WHERE id=$1 AND owner_id=$2",
            callback_data.session_id,
            callback.from_user.id,
        )
    except Exception as exc:
        mark_handled_error(f"ract_delete: {exc}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка удаления:</b> <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_ract_back_kb().as_markup(),
        )
        return
    await callback.message.edit_text(
        "🗑 <b>Сессия удалена</b>",
        parse_mode="HTML",
        reply_markup=_ract_back_kb().as_markup(),
    )
