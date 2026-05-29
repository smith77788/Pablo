"""
Infrastructure Analytics — аналитика Telegram-инфраструктуры.

Показывает:
- Сводка здоровья аккаунтов (health scores, warmup states)
- Flood Intelligence — история flood events, риск-рейтинг аккаунтов
- Operation Audit — лог выполненных операций
- Account capability overview
- Daily stats по аккаунтам

Entry point: InfraCb(action="menu")
"""
from __future__ import annotations

import html
import logging
from datetime import date, timedelta

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import InfraCb

log = logging.getLogger(__name__)
router = Router()

_PAGE = 10


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=InfraCb(action="menu"))
    return kb


# ── Главное меню аналитики ────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "menu"))
async def cb_infra_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    # Быстрые метрики
    acc_total = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", uid
    ) or 0
    floods_24h = await pool.fetchval(
        """SELECT COUNT(*) FROM account_flood_log fl
           JOIN tg_accounts a ON a.id=fl.account_id
           WHERE a.owner_id=$1 AND fl.created_at > NOW() - INTERVAL '24h'""",
        uid,
    ) or 0
    ops_today = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND created_at > NOW() - INTERVAL '24h'",
        uid,
    ) or 0
    warmup_active = await pool.fetchval(
        """SELECT COUNT(*) FROM account_warmup_plans wp
           JOIN tg_accounts a ON a.id=wp.account_id
           WHERE a.owner_id=$1 AND wp.status='active'""",
        uid,
    ) or 0

    kb = InlineKeyboardBuilder()
    kb.button(text="❤️ Здоровье аккаунтов",    callback_data=InfraCb(action="health"))
    kb.button(text="⚡ Flood Intelligence",      callback_data=InfraCb(action="flood"))
    kb.button(text="📋 Лог операций",            callback_data=InfraCb(action="audit"))
    kb.button(text="📊 Статистика за сегодня",   callback_data=InfraCb(action="daily_stats"))
    kb.button(text="🎯 Возможности аккаунтов",   callback_data=InfraCb(action="capabilities"))
    kb.adjust(1)

    await callback.message.edit_text(
        "📊 <b>Infrastructure Analytics</b>\n\n"
        f"🤖 Активных аккаунтов: <b>{acc_total}</b>\n"
        f"⚡ Flood событий (24ч): <b>{floods_24h}</b>\n"
        f"⚙️ Операций сегодня: <b>{ops_today}</b>\n"
        f"🌡 Разогревается: <b>{warmup_active}</b>\n\n"
        "Выберите раздел для детального просмотра:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Здоровье аккаунтов ────────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "health"))
async def cb_infra_health(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.account_health import load_from_db, get_health_summary

    uid = callback.from_user.id
    await load_from_db(pool, uid)

    accounts = await pool.fetch(
        "SELECT id, first_name, phone, trust_score, acc_status FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY trust_score DESC NULLS LAST",
        uid,
    )

    if not accounts:
        await callback.message.edit_text(
            "❤️ <b>Здоровье аккаунтов</b>\n\nАккаунтов нет.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    acc_ids = [a["id"] for a in accounts]
    summary = get_health_summary(acc_ids)

    lines = ["❤️ <b>Здоровье аккаунтов</b>\n"]
    for s, acc in zip(summary[:15], accounts[:15]):
        label = html.escape(acc.get("first_name") or acc["phone"])
        score = s["health_score"]
        icon = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
        warmup = s["warmup_state"]
        lines.append(
            f"{icon} <b>{label}</b>: {score}% | {warmup} | trust={acc['trust_score']:.1f}"
        )
    if len(accounts) > 15:
        lines.append(f"\n<i>...и ещё {len(accounts)-15} аккаунтов</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Flood Intelligence ────────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "flood"))
async def cb_infra_flood(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.flood_engine import get_risk_summary

    uid = callback.from_user.id
    rows = await pool.fetch(
        """SELECT a.id, a.first_name, a.phone,
                  COUNT(fl.id) AS floods_total,
                  COUNT(fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '24h') AS floods_24h,
                  COUNT(fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '7d') AS floods_7d,
                  MAX(fl.created_at) AS last_flood
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id=a.id
           WHERE a.owner_id=$1 AND a.is_active=TRUE
           GROUP BY a.id, a.first_name, a.phone
           ORDER BY floods_24h DESC, floods_7d DESC""",
        uid,
    )

    lines = ["⚡ <b>Flood Intelligence</b>\n"]
    risk_summary = get_risk_summary([r["id"] for r in rows])

    for row in rows[:15]:
        label = html.escape(row.get("first_name") or row["phone"])
        f24 = row["floods_24h"] or 0
        f7d = row["floods_7d"] or 0
        risk = risk_summary.get(row["id"], {})
        risk_score = risk.get("risk_score", 0)
        cooling = "⏳ охлаждается" if risk.get("is_cooling") else ""
        icon = "🔴" if f24 >= 3 else ("🟡" if f24 >= 1 else "🟢")
        lines.append(
            f"{icon} <b>{label}</b>: 24ч={f24} 7д={f7d} риск={risk_score} {cooling}"
        )

    # Топ action-типов
    action_stats = await pool.fetch(
        """SELECT action_type, COUNT(*) as cnt
           FROM account_flood_log fl
           JOIN tg_accounts a ON a.id=fl.account_id
           WHERE a.owner_id=$1 AND fl.created_at > NOW() - INTERVAL '7d'
           GROUP BY action_type ORDER BY cnt DESC LIMIT 5""",
        uid,
    )
    if action_stats:
        lines.append("\n<b>Топ действий с flood (7 дней):</b>")
        for s in action_stats:
            lines.append(f"  • {s['action_type'] or 'default'}: {s['cnt']} flood")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Operation Audit Log ───────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "audit"))
async def cb_infra_audit(
    callback: CallbackQuery, callback_data: InfraCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    page = callback_data.page
    uid = callback.from_user.id

    rows = await pool.fetch(
        """SELECT oa.action, oa.target, oa.result, oa.error_msg,
                  oa.flood_wait_s, oa.duration_ms, oa.occurred_at,
                  a.first_name, a.phone
           FROM operation_audit oa
           LEFT JOIN tg_accounts a ON a.id=oa.account_id
           WHERE oa.owner_id=$1
           ORDER BY oa.occurred_at DESC
           OFFSET $2 LIMIT $3""",
        uid, page * _PAGE, _PAGE,
    )

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_audit WHERE owner_id=$1", uid
    ) or 0

    if not rows:
        await callback.message.edit_text(
            "📋 <b>Лог операций пуст</b>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = [f"📋 <b>Лог операций</b> (всего: {total:,})\n"]
    for r in rows:
        icon = {"success": "✅", "error": "❌", "flood_wait": "⏳", "banned": "🚫"}.get(r["result"], "❓")
        acc_label = html.escape(r.get("first_name") or r.get("phone") or "?")
        target = html.escape((r.get("target") or "")[:20])
        t = r["occurred_at"].strftime("%m-%d %H:%M") if r["occurred_at"] else "?"
        dur = f" {r['duration_ms']}ms" if r.get("duration_ms") else ""
        lines.append(f"{icon} [{t}] <code>{r['action']}</code> {target} [{acc_label}]{dur}")

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="◀️", callback_data=InfraCb(action="audit", page=page-1))
    if (page + 1) * _PAGE < total:
        kb.button(text="▶️", callback_data=InfraCb(action="audit", page=page+1))
    kb.button(text="◀️ Назад", callback_data=InfraCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Daily Stats ────────────────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "daily_stats"))
async def cb_infra_daily_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    today = date.today()

    rows = await pool.fetch(
        """SELECT a.first_name, a.phone,
                  COALESCE(ds.actions_ok, 0) AS actions_ok,
                  COALESCE(ds.actions_fail, 0) AS actions_fail,
                  COALESCE(ds.flood_events, 0) AS flood_events,
                  COALESCE(ds.messages_sent, 0) AS messages_sent
           FROM tg_accounts a
           LEFT JOIN account_daily_stats ds ON ds.account_id=a.id AND ds.stat_date=$2
           WHERE a.owner_id=$1 AND a.is_active=TRUE
           ORDER BY (COALESCE(ds.actions_ok,0) + COALESCE(ds.messages_sent,0)) DESC""",
        uid, today,
    )

    lines = [f"📊 <b>Статистика за {today.strftime('%d.%m.%Y')}</b>\n"]
    total_ok = total_fail = total_floods = total_msgs = 0

    for r in rows[:20]:
        label = html.escape(r.get("first_name") or r["phone"])
        ok = r["actions_ok"]
        fail = r["actions_fail"]
        floods = r["flood_events"]
        msgs = r["messages_sent"]
        total_ok += ok; total_fail += fail; total_floods += floods; total_msgs += msgs
        if ok + fail + msgs > 0:
            lines.append(f"• <b>{label}</b>: ✅{ok} ❌{fail} ⚡{floods} ✉️{msgs}")

    lines.append(
        f"\n<b>Итого:</b> ✅{total_ok} ❌{total_fail} ⚡{total_floods} ✉️{total_msgs}"
    )

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Account Capabilities ──────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "capabilities"))
async def cb_infra_capabilities(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    rows = await pool.fetch(
        """SELECT a.first_name, a.phone, a.trust_score,
                  COALESCE(ac.can_invite, TRUE) AS can_invite,
                  COALESCE(ac.can_dm, TRUE) AS can_dm,
                  COALESCE(ac.can_create_channel, TRUE) AS can_create,
                  COALESCE(ac.is_premium, FALSE) AS is_premium,
                  COALESCE(ac.daily_dm_limit, 50) AS dm_limit,
                  ac.last_discovery
           FROM tg_accounts a
           LEFT JOIN account_capabilities ac ON ac.account_id=a.id
           WHERE a.owner_id=$1 AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST""",
        uid,
    )

    if not rows:
        await callback.message.edit_text(
            "🎯 <b>Возможности аккаунтов</b>\n\nАккаунтов нет.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = ["🎯 <b>Возможности аккаунтов</b>\n"]
    for r in rows[:15]:
        label = html.escape(r.get("first_name") or r["phone"])
        caps = []
        if r["can_invite"]:  caps.append("📨inv")
        if r["can_dm"]:      caps.append("✉️dm")
        if r["can_create"]:  caps.append("📡crt")
        if r["is_premium"]:  caps.append("⭐prm")
        discovered = "❓" if not r["last_discovery"] else "✅"
        lines.append(f"{discovered} <b>{label}</b>: {' '.join(caps) or 'нет данных'} DM-лимит:{r['dm_limit']}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить возможности", callback_data=InfraCb(action="discover_caps"))
    kb.button(text="◀️ Назад",                callback_data=InfraCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(InfraCb.filter(F.action == "discover_caps"))
async def cb_discover_capabilities(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Базовое определение возможностей через acc_status и trust_score."""
    await callback.answer("🔄 Обновляю...")
    uid = callback.from_user.id

    accounts = await pool.fetch(
        "SELECT id, acc_status, trust_score FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        uid,
    )

    updated = 0
    for acc in accounts:
        status = acc.get("acc_status") or "active"
        trust = float(acc.get("trust_score") or 1.0)

        # Логика на основе статуса и trust_score
        can_dm     = status in ("active",) and trust >= 0.3
        can_invite = status in ("active",) and trust >= 0.5
        can_create = status in ("active",) and trust >= 0.2
        dm_limit   = 50 if trust >= 0.7 else (20 if trust >= 0.4 else 5)

        try:
            await pool.execute(
                """INSERT INTO account_capabilities(
                       account_id, owner_id, can_invite, can_dm,
                       can_create_channel, daily_dm_limit, last_discovery
                   ) VALUES ($1,$2,$3,$4,$5,$6,NOW())
                   ON CONFLICT(account_id) DO UPDATE
                   SET can_invite=$3, can_dm=$4, can_create_channel=$5,
                       daily_dm_limit=$6, last_discovery=NOW()""",
                acc["id"], uid, can_invite, can_dm, can_create, dm_limit,
            )
            updated += 1
        except Exception as e:
            log.debug("discover_caps acc=%d: %s", acc["id"], e)

    await callback.message.edit_text(
        f"✅ <b>Возможности обновлены</b>\n\n"
        f"Обновлено аккаунтов: <b>{updated}</b>\n\n"
        "<i>Базовая оценка по статусу и trust_score.\n"
        "Для точной проверки используйте «🔍 Проверить все» в разделе Аккаунты.</i>",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )
