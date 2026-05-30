"""
Account Warmup UI — управление планами разогрева аккаунтов.

Разогрев нужен для новых аккаунтов: имитирует натуральную активность
перед боевыми операциями, повышает trust_score, снижает риск блокировок.
"""

from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import WarmupCb, BmCb

log = logging.getLogger(__name__)
router = Router()

_PLAN_LABELS = {
    "gentle": "🌱 Gentle (21 день, 3 действия/день)",
    "standard": "🌿 Standard (14 дней, 5 действий/день)",
    "aggressive": "🔥 Aggressive (7 дней, 10 действий/день)",
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
    kb.button(text="◀️ Назад", callback_data=BmCb(action="infrastructure"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🌡 <b>Account Warming — Разогрев аккаунтов</b>\n\n"
        "Безопасный разогрев новых аккаунтов перед работой.\n"
        "Имитирует натуральную активность: чтение, реакции, поиск.\n\n"
        f"Активных планов: <b>{active_count}</b>\n\n"
        "<b>Режимы:</b>\n"
        "🌱 Gentle — осторожный, 21 день\n"
        "🌿 Standard — сбалансированный, 14 дней\n"
        "🔥 Aggressive — быстрый, 7 дней",
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

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        icon = "✅" if acc["has_plan"] else "⚪"
        label = acc.get("first_name") or acc["phone"]
        kb.button(
            text=f"{icon} {html.escape(label)} [{acc['acc_status']}]",
            callback_data=WarmupCb(action="select_plan", account_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "📱 <b>Выберите аккаунт для разогрева:</b>\n\n"
        "✅ = уже есть активный план\n"
        "⚪ = план отсутствует",
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
    for plan in plans:
        label = plan.get("first_name") or plan.get("phone") or str(plan["account_id"])
        pct = round(plan["current_day"] / max(plan["target_days"], 1) * 100)
        bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
        lines.append(
            f"• <b>{html.escape(label)}</b>\n"
            f"  [{bar}] День {plan['current_day']}/{plan['target_days']}\n"
            f"  Режим: {plan['plan_type']} | {plan['daily_actions']} действий/день"
        )
        kb.button(
            text=f"🗑 Удалить план {label[:15]}",
            callback_data=WarmupCb(action="delete_plan", plan_id=plan["id"]),
        )

    kb.button(text="◀️ Назад", callback_data=WarmupCb(action="menu"))
    kb.adjust(1)

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
    await callback.answer("⏳ Запускаю...")
    from services.account_warmer import get_active_plans, run_daily_warmup

    plans = await get_active_plans(pool, callback.from_user.id)
    if not plans:
        await callback.message.edit_text(
            "⚠️ Нет активных планов разогрева.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    results = []
    for plan in plans:
        label = plan.get("first_name") or plan.get("phone") or str(plan["account_id"])
        res = await run_daily_warmup(pool, plan)
        status = "✅" if res["actions_ok"] > 0 else "⚠️"
        results.append(
            f"{status} <b>{html.escape(label)}</b>: "
            f"{res['actions_ok']}/{res['actions_done']} успешно"
            + (" 🏁 завершён!" if res["completed"] else "")
        )

    await callback.message.edit_text(
        "🌡 <b>Сеанс разогрева завершён</b>\n\n" + "\n".join(results),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )
