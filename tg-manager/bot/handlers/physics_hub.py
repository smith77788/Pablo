"""Physics Engine UI — account risk scores and safety envelopes."""

from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, PhysicsCb
from services import physics_engine

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 8


async def _menu_text_kb(
    pool: asyncpg.Pool,
    user_id: int,
    page: int = 0,
):
    """Build Physics Engine menu: list user's accounts with risk scores."""
    try:
        accounts = await pool.fetch(
            """SELECT a.id, a.phone, a.username, a.first_name,
                      r.risk_score, r.ban_probability, r.ops_24h, r.flood_rate_1h
               FROM tg_accounts a
               LEFT JOIN account_risk_scores r ON r.account_id = a.id
               WHERE a.owner_id=$1 AND a.banned=FALSE
               ORDER BY COALESCE(r.risk_score, 0) DESC, a.id
               LIMIT $2 OFFSET $3""",
            user_id,
            _PAGE_SIZE,
            page * _PAGE_SIZE,
        )
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND banned=FALSE",
            user_id,
        )
    except Exception as e:
        log.debug("physics_hub._menu_text_kb: %s", e)
        accounts = []
        total = 0

    total = int(total or 0)
    pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    try:
        global_row = await pool.fetchrow(
            """SELECT
               AVG(risk_score)      AS avg_risk,
               COUNT(*) FILTER (WHERE risk_score >= 0.75) AS critical,
               COUNT(*) FILTER (WHERE risk_score >= 0.5 AND risk_score < 0.75) AS high
               FROM account_risk_scores
               WHERE account_id IN (
                   SELECT id FROM tg_accounts WHERE owner_id=$1 AND banned=FALSE
               )""",
            user_id,
        )
    except Exception:
        global_row = None

    lines = ["⚛️ <b>Physics Engine</b>\n"]
    if global_row and global_row["avg_risk"] is not None:
        avg   = round(float(global_row["avg_risk"]), 2)
        crit  = int(global_row["critical"] or 0)
        high_ = int(global_row["high"] or 0)
        lines.append(f"Средний риск по парку: <b>{avg:.0%}</b>")
        if crit:
            lines.append(f"🔴 Критических: <b>{crit}</b>")
        if high_:
            lines.append(f"🟠 Высокий риск: <b>{high_}</b>")
        lines.append("")

    if not accounts:
        lines.append("<i>Нет данных. Запустите операции — Physics Engine накопит телеметрию.</i>")
    else:
        for acc in accounts:
            risk  = acc["risk_score"]
            label = physics_engine.risk_label(risk if risk is not None else 0.0)
            name  = (
                f"@{acc['username']}" if acc.get("username")
                else (acc.get("first_name") or acc.get("phone") or f"id{acc['id']}")
            )
            ban_p = acc["ban_probability"]
            ops   = acc["ops_24h"] or 0
            line  = f"{label} <b>{name}</b> · {ops} оп/24ч"
            if ban_p and ban_p > 0.05:
                line += f" · ⚠️ бан {ban_p:.0%}"
            lines.append(line)

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        risk  = acc["risk_score"]
        score = risk if risk is not None else 0.0
        em    = "🟢" if score < 0.25 else "🟡" if score < 0.5 else "🟠" if score < 0.75 else "🔴"
        name  = (
            f"@{acc['username']}" if acc.get("username")
            else (acc.get("first_name") or acc.get("phone") or f"id{acc['id']}")
        )[:20]
        kb.button(
            text=f"{em} {name}",
            callback_data=PhysicsCb(action="detail", account_id=acc["id"], page=page),
        )
    kb.adjust(2)

    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardBuilder().button(
                text="◀️", callback_data=PhysicsCb(action="menu", page=page - 1)
            )
        )
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(
            InlineKeyboardBuilder().button(
                text="▶️", callback_data=PhysicsCb(action="menu", page=page + 1)
            )
        )
    if pages > 1:
        kb.button(
            text=f"📄 {page + 1}/{pages}",
            callback_data=PhysicsCb(action="menu", page=page),
        )
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(2, *(1 for _ in range(10)))  # accounts 2-wide, rest 1-wide

    return text, kb.as_markup()


@router.callback_query(PhysicsCb.filter(F.action == "menu"))
async def cb_physics_menu(
    callback: CallbackQuery,
    callback_data: PhysicsCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    text, markup = await _menu_text_kb(pool, callback.from_user.id, callback_data.page)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(PhysicsCb.filter(F.action == "detail"))
async def cb_physics_detail(
    callback: CallbackQuery,
    callback_data: PhysicsCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    account_id = callback_data.account_id

    try:
        acc = await pool.fetchrow(
            "SELECT id, phone, username, first_name FROM tg_accounts WHERE id=$1",
            account_id,
        )
    except Exception:
        acc = None

    risk = await physics_engine.get_account_risk(pool, account_id)

    name = "Неизвестный аккаунт"
    if acc:
        name = (
            f"@{acc['username']}" if acc.get("username")
            else (acc.get("first_name") or acc.get("phone") or f"id{acc['id']}")
        )

    score    = risk["risk_score"]
    ban_prob = risk["ban_probability"]
    ops_24h  = risk["ops_24h"]
    flood_r  = risk["flood_rate_1h"]
    label    = physics_engine.risk_label(score)
    safe_ops = physics_engine.safe_ops_per_hour(score)

    last_flood_str = "—"
    if risk["last_flood_at"]:
        last_flood_str = risk["last_flood_at"].strftime("%d.%m %H:%M")

    # Recent telemetry breakdown
    try:
        breakdown = await pool.fetch(
            """SELECT op_type, outcome, COUNT(*) AS cnt
               FROM op_telemetry
               WHERE account_id=$1 AND created_at > NOW() - INTERVAL '24 hours'
               GROUP BY op_type, outcome
               ORDER BY cnt DESC
               LIMIT 8""",
            account_id,
        )
    except Exception:
        breakdown = []

    lines = [
        f"⚛️ <b>Physics — {name}</b>\n",
        f"Риск: {label} (<b>{score:.0%}</b>)",
        f"Вероятность бана 7д: <b>{ban_prob:.0%}</b>",
        f"Операций за 24ч: <b>{ops_24h}</b>",
        f"Flood rate (1ч): <b>{flood_r:.0%}</b>",
        f"Последний FloodWait: <b>{last_flood_str}</b>",
        f"Рекомендуемый темп: <b>≤{safe_ops} оп/ч</b>",
    ]

    if breakdown:
        lines.append("\n<b>Телеметрия (24ч):</b>")
        outcome_icons = {
            "success": "✅", "flood_wait": "⏳", "ban": "🚫", "error": "❌"
        }
        for row in breakdown:
            icon = outcome_icons.get(row["outcome"], "·")
            lines.append(f"  {icon} {row['op_type']}: {row['cnt']}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад",
        callback_data=PhysicsCb(action="menu", page=callback_data.page),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
