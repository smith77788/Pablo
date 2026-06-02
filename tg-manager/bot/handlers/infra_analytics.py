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
from services import infra_pressure
from database import db as _db

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

    # Infrastructure Pressure Score
    pressure = await infra_pressure.compute_pressure(pool, uid)
    p_emoji = pressure.get("level_emoji", "🟢")
    p_score = pressure.get("score", 0)
    p_label = pressure.get("level_label", "Норма")

    # Distinct pools count
    pool_count = await pool.fetchval(
        "SELECT COUNT(DISTINCT pool) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND pool IS NOT NULL",
        uid,
    ) or 0

    kb = InlineKeyboardBuilder()
    kb.button(text="🗂️ Реестр ассетов",         callback_data=InfraCb(action="asset_registry"))
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
        f"🌡 Разогревается: <b>{warmup_active}</b>\n"
        f"🏊 Пулов: <b>{pool_count}</b>\n\n"
        f"{p_emoji} Давление инфраструктуры: <b>{p_score}/100</b> — {p_label}\n\n"
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
            f"{icon} <b>{label}</b>: {score}% | {warmup} | trust={float(acc['trust_score'] or 0):.1f}"
        )
    if len(accounts) > 15:
        lines.append(f"\n<i>...и ещё {len(accounts)-15} аккаунтов</i>")

    # Per-pool breakdown
    pool_rows = await pool.fetch(
        """SELECT pool, COUNT(*) AS cnt, AVG(trust_score) AS avg_trust
           FROM tg_accounts
           WHERE owner_id=$1 AND is_active=TRUE
           GROUP BY pool
           ORDER BY pool""",
        uid,
    )
    if pool_rows:
        lines.append("\n<b>📊 По пулам:</b>")
        for pr in pool_rows:
            pool_name = pr["pool"] or "<i>без пула</i>"
            avg_t = float(pr["avg_trust"] or 0)
            lines.append(f"  🏊 {pool_name}: {pr['cnt']} акк, avg trust={avg_t:.2f}")

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

    # Bad proxies (success_rate < 50%)
    bad_proxy_count = 0
    try:
        bad_proxy_count = await pool.fetchval(
            """SELECT COUNT(DISTINCT up.id)
               FROM user_proxies up
               JOIN (
                   SELECT proxy_id,
                          SUM(CASE WHEN success THEN 1 ELSE 0 END)::float / NULLIF(COUNT(*), 0) AS success_rate
                   FROM proxy_quality_log
                   GROUP BY proxy_id
               ) q ON q.proxy_id = up.id
               WHERE up.owner_id=$1 AND q.success_rate < 0.5""",
            uid,
        ) or 0
    except Exception:
        pass

    if not rows:
        proxy_warn = f"\n⚠️ Плохих прокси (< 50% успех): <b>{bad_proxy_count}</b>" if bad_proxy_count > 0 else ""
        await callback.message.edit_text(
            f"📋 <b>Лог операций пуст</b>{proxy_warn}",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = [f"📋 <b>Лог операций</b> (всего: {total:,})\n"]
    if bad_proxy_count > 0:
        lines.append(f"⚠️ Плохих прокси (< 50% успех): <b>{bad_proxy_count}</b>\n")
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


# ── Unified Asset Registry ─────────────────────────────────────────────────────

@router.callback_query(InfraCb.filter(F.action == "asset_registry"))
async def cb_asset_registry(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Единый реестр всех ассетов пользователя с агрегированной статистикой."""
    from bot.utils.subscription import require_plan
    from bot.keyboards import subscription_locked_markup
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            "🔒 <b>Реестр ассетов — Starter+</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    await callback.answer()
    uid = callback.from_user.id

    # Parallel aggregation queries
    acc_row = await pool.fetchrow(
        """SELECT COUNT(*) AS total,
                  COUNT(CASE WHEN is_active THEN 1 END) AS active,
                  COUNT(CASE WHEN cooldown_until > now() THEN 1 END) AS in_cooldown,
                  ROUND(AVG(COALESCE(trust_score, 1.0))::numeric, 2) AS avg_trust
           FROM tg_accounts WHERE owner_id=$1""",
        uid,
    )
    bot_row = await pool.fetchrow(
        """SELECT COUNT(*) AS total,
                  COUNT(CASE WHEN is_active THEN 1 END) AS active,
                  COALESCE(SUM(u.cnt), 0) AS total_users
           FROM managed_bots b
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) u ON u.bot_id = b.bot_id
           WHERE b.added_by=$1""",
        uid,
    )

    # Channels and groups via managed_channels
    try:
        chan_total = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid
        ) or 0
    except Exception:
        chan_total = 0

    try:
        group_total = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND type IN ('megagroup','supergroup','group')", uid
        ) or 0
    except Exception:
        group_total = 0

    try:
        cluster_total = await pool.fetchval(
            "SELECT COUNT(*) FROM clusters WHERE owner_id=$1", uid
        ) or 0
    except Exception:
        cluster_total = 0

    try:
        funnel_total = await pool.fetchval(
            """SELECT COUNT(*) FROM funnels f
               JOIN managed_bots b ON b.bot_id=f.bot_id
               WHERE b.added_by=$1 AND f.is_active=TRUE""",
            uid,
        ) or 0
    except Exception:
        funnel_total = 0

    try:
        keyword_total = await pool.fetchval(
            "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE", uid
        ) or 0
    except Exception:
        keyword_total = 0

    try:
        proxy_total = await pool.fetchval(
            "SELECT COUNT(*) FROM proxies WHERE owner_id=$1", uid
        ) or 0
    except Exception:
        proxy_total = 0

    try:
        template_total = await pool.fetchval(
            "SELECT COUNT(*) FROM asset_templates WHERE owner_id=$1", uid
        ) or 0
    except Exception:
        template_total = 0

    acc = acc_row or {}
    bot = bot_row or {}

    lines = [
        "🗂️ <b>Unified Asset Registry</b>\n",
        "<b>📱 Аккаунты</b>",
        f"   Всего: <b>{acc.get('total', 0)}</b>  |  "
        f"Активных: <b>{acc.get('active', 0)}</b>  |  "
        f"Кулдаун: <b>{acc.get('in_cooldown', 0)}</b>",
        f"   Avg trust: <b>{acc.get('avg_trust', 0)}</b>",
        "",
        "<b>🤖 Боты</b>",
        f"   Всего: <b>{bot.get('total', 0)}</b>  |  "
        f"Активных: <b>{bot.get('active', 0)}</b>",
        f"   Аудитория: <b>{int(bot.get('total_users', 0)):,}</b> пользователей",
        "",
        "<b>📡 Каналы</b>",
        f"   Подключено: <b>{chan_total}</b>",
        "",
        "<b>👥 Группы</b>",
        f"   Активных: <b>{group_total}</b>",
        "",
        "<b>📊 Другие активы</b>",
        f"   🔗 Кластеры: <b>{cluster_total}</b>  |  "
        f"🌐 Прокси: <b>{proxy_total}</b>",
        f"   🔄 Воронки: <b>{funnel_total}</b>  |  "
        f"📋 Шаблоны: <b>{template_total}</b>",
        f"   🔍 Ключевых слов: <b>{keyword_total}</b>",
    ]

    from bot.callbacks import (
        AccCb, BotCb, ChanCb, GroupFCb,
        ClustMCb, ProxyCb, FunnelCb, AssetTplCb,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Аккаунты",     callback_data=AccCb(action="menu"))
    kb.button(text="🤖 Боты",         callback_data=BotCb(action="list", page=0))
    kb.button(text="📡 Каналы",       callback_data=ChanCb(action="menu"))
    kb.button(text="👥 Группы",       callback_data=GroupFCb(action="menu"))
    kb.button(text="🔗 Кластеры",     callback_data=ClustMCb(action="menu"))
    kb.button(text="🌐 Прокси",       callback_data=ProxyCb(action="menu"))
    kb.button(text="📋 Шаблоны",      callback_data=AssetTplCb(action="menu"))
    kb.button(text="◀️ Назад",        callback_data=InfraCb(action="menu"))
    kb.adjust(2, 2, 2, 1, 1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
