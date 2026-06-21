"""Account Shield Hub — проактивная защита аккаунтов от бана.

Entry: ShieldCb(action="menu")

Экраны:
  menu       — общий статус: угрозы / охлаждение / OK
  top10      — топ-10 рискованных аккаунтов
  settings   — настройки Shield (порог, auto_pause)
  history    — история действий за 7 дней
  toggle_ap  — переключить auto_pause
  toggle_na  — переключить notify_admin
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ShieldCb, BmCb
from services import account_shield
from services import physics_engine

log = logging.getLogger(__name__)
router = Router()

_ACTION_LABEL = {
    "ok":    "✅ OK",
    "warn":  "⚠️ Предупреждение",
    "cool":  "❄️ Охлаждение",
    "pause": "⏸ Пауза",
}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ShieldCb(action="menu"))
    return kb


async def _get_summary(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Return aggregated shield stats for an owner."""
    try:
        row = await pool.fetchrow(
            """SELECT
               COUNT(*) FILTER (WHERE a.is_active = TRUE) AS total_active,
               COUNT(*) FILTER (WHERE a.is_active = FALSE AND
                   a.cooldown_until > NOW()) AS paused_by_shield,
               COUNT(*) FILTER (WHERE r.risk_score >= 0.7) AS threatened,
               COUNT(*) FILTER (WHERE r.ban_probability >= 0.5) AS high_ban_prob,
               COUNT(*) FILTER (WHERE r.risk_score < 0.7 AND
                   (r.ban_probability < 0.5 OR r.ban_probability IS NULL)) AS ok_count
               FROM tg_accounts a
               LEFT JOIN account_risk_scores r ON r.account_id = a.id
               WHERE a.owner_id = $1""",
            owner_id,
        )
    except Exception as exc:
        log.debug("shield_hub._get_summary: %s", exc)
        row = None

    return {
        "total_active":    int((row["total_active"] or 0) if row else 0),
        "paused_by_shield": int((row["paused_by_shield"] or 0) if row else 0),
        "threatened":      int((row["threatened"] or 0) if row else 0),
        "high_ban_prob":   int((row["high_ban_prob"] or 0) if row else 0),
        "ok_count":        int((row["ok_count"] or 0) if row else 0),
    }


# ─── Меню ─────────────────────────────────────────────────────────────────────


@router.callback_query(ShieldCb.filter(F.action == "menu"))
async def cb_shield_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    stats = await _get_summary(pool, owner_id)
    cfg = await account_shield.get_shield_config(pool, owner_id)

    lines = [
        "🛡 <b>Account Shield — проактивная защита</b>\n",
        f"📱 Активных аккаунтов: <b>{stats['total_active']}</b>",
        f"🔴 Под угрозой (risk ≥ {cfg.risk_threshold:.0%}): <b>{stats['threatened']}</b>",
        f"⚠️ Высокая вероятность бана: <b>{stats['high_ban_prob']}</b>",
        f"⏸ На паузе (Shield): <b>{stats['paused_by_shield']}</b>",
        f"✅ В безопасности: <b>{stats['ok_count']}</b>",
        "",
        f"⚙️ Авто-пауза: {'<b>вкл</b>' if cfg.auto_pause else '<b>выкл</b>'}  "
        f"Уведомления: {'<b>вкл</b>' if cfg.notify_admin else '<b>выкл</b>'}",
    ]
    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Топ-10 рискованных", callback_data=ShieldCb(action="top10"))
    kb.button(text="⚙️ Настройки", callback_data=ShieldCb(action="settings"))
    kb.button(text="📋 История (7 дней)", callback_data=ShieldCb(action="history"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(2, 1, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ─── Топ-10 рискованных ───────────────────────────────────────────────────────


@router.callback_query(ShieldCb.filter(F.action == "top10"))
async def cb_shield_top10(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT a.id, a.phone, a.first_name, a.username,
                      r.risk_score, r.ban_probability, r.ops_24h, r.flood_rate_1h
               FROM tg_accounts a
               JOIN account_risk_scores r ON r.account_id = a.id
               WHERE a.owner_id = $1
               ORDER BY r.risk_score DESC, r.ban_probability DESC
               LIMIT 10""",
            owner_id,
        )
    except Exception as exc:
        log.debug("shield_hub.top10: %s", exc)
        rows = []

    lines = ["📊 <b>Топ-10 рискованных аккаунтов</b>\n"]
    if not rows:
        lines.append("<i>Нет данных — Physics Engine ещё не накопил телеметрию.</i>")
    else:
        for i, acc in enumerate(rows, 1):
            risk = float(acc["risk_score"] or 0.0)
            ban_p = float(acc["ban_probability"] or 0.0)
            label = physics_engine.risk_label(risk)
            name = (
                f"@{acc['username']}"
                if acc.get("username")
                else (acc.get("first_name") or acc.get("phone") or f"id{acc['id']}")
            )
            ops = int(acc["ops_24h"] or 0)
            lines.append(
                f"{i}. {label} <b>{html.escape(str(name))}</b>\n"
                f"   risk <b>{risk:.0%}</b> · бан <b>{ban_p:.0%}</b> · {ops} оп/24ч"
            )

    kb = _back_kb()
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ─── Настройки ────────────────────────────────────────────────────────────────


@router.callback_query(ShieldCb.filter(F.action == "settings"))
async def cb_shield_settings(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    cfg = await account_shield.get_shield_config(pool, owner_id)

    lines = [
        "⚙️ <b>Настройки Account Shield</b>\n",
        f"🔺 Порог риска (risk_threshold): <b>{cfg.risk_threshold:.0%}</b>",
        f"⚠️ Порог вероятности бана: <b>{cfg.ban_prob_threshold:.0%}</b>",
        f"⏸ Авто-пауза: {'<b>вкл</b>' if cfg.auto_pause else '<b>выкл</b>'}",
        f"🔔 Уведомления: {'<b>вкл</b>' if cfg.notify_admin else '<b>выкл</b>'}",
        f"⏱ Длительность паузы: <b>{cfg.cool_duration_hours}ч</b>",
    ]
    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    ap_label = "⏸ Авто-пауза: ВЫКЛ" if cfg.auto_pause else "⏸ Авто-пауза: ВКЛ"
    na_label = "🔔 Уведомления: ВЫКЛ" if cfg.notify_admin else "🔔 Уведомления: ВКЛ"
    kb.button(text=ap_label, callback_data=ShieldCb(action="toggle_ap"))
    kb.button(text=na_label, callback_data=ShieldCb(action="toggle_na"))
    kb.button(text="◀️ Назад", callback_data=ShieldCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ShieldCb.filter(F.action == "toggle_ap"))
async def cb_shield_toggle_ap(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    cfg = await account_shield.get_shield_config(pool, owner_id)
    new_val = not cfg.auto_pause
    try:
        await pool.execute(
            """INSERT INTO shield_configs (owner_id, auto_pause)
               VALUES ($1, $2)
               ON CONFLICT (owner_id) DO UPDATE SET auto_pause=$2, updated_at=NOW()""",
            owner_id,
            new_val,
        )
    except Exception as exc:
        log.debug("shield_hub.toggle_ap: %s", exc)
    # Show updated settings
    await cb_shield_settings.__wrapped__(callback, pool)  # type: ignore[attr-defined]


@router.callback_query(ShieldCb.filter(F.action == "toggle_na"))
async def cb_shield_toggle_na(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    cfg = await account_shield.get_shield_config(pool, owner_id)
    new_val = not cfg.notify_admin
    try:
        await pool.execute(
            """INSERT INTO shield_configs (owner_id, notify_admin)
               VALUES ($1, $2)
               ON CONFLICT (owner_id) DO UPDATE SET notify_admin=$2, updated_at=NOW()""",
            owner_id,
            new_val,
        )
    except Exception as exc:
        log.debug("shield_hub.toggle_na: %s", exc)
    await cb_shield_settings.__wrapped__(callback, pool)  # type: ignore[attr-defined]


# ─── История ──────────────────────────────────────────────────────────────────


@router.callback_query(ShieldCb.filter(F.action == "history"))
async def cb_shield_history(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT sa.action, sa.risk_score, sa.ban_probability,
                      sa.note, sa.created_at,
                      a.phone, a.first_name, a.username
               FROM shield_actions sa
               LEFT JOIN tg_accounts a ON a.id = sa.account_id
               WHERE sa.owner_id = $1
                 AND sa.created_at > NOW() - INTERVAL '7 days'
               ORDER BY sa.created_at DESC
               LIMIT 50""",
            owner_id,
        )
    except Exception as exc:
        log.debug("shield_hub.history: %s", exc)
        rows = []

    lines = ["📋 <b>История Shield (7 дней)</b>\n"]
    if not rows:
        lines.append("<i>Действий не зафиксировано.</i>")
    else:
        for row in rows:
            action_label = _ACTION_LABEL.get(row["action"], row["action"])
            name = (
                f"@{row['username']}"
                if row.get("username")
                else (row.get("first_name") or row.get("phone") or "—")
            )
            ts = row["created_at"]
            if ts and hasattr(ts, "strftime"):
                ts_str = ts.strftime("%d.%m %H:%M")
            else:
                ts_str = str(ts)[:16] if ts else "—"
            risk = float(row["risk_score"] or 0.0)
            ban_p = float(row["ban_probability"] or 0.0)
            lines.append(
                f"{ts_str} {action_label} — <b>{html.escape(str(name))}</b>"
                f" (risk {risk:.0%} / бан {ban_p:.0%})"
            )

    kb = _back_kb()
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )
