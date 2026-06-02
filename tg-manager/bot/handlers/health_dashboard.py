"""Health Dashboard — infrastructure health monitoring.

Entry point: HealthCb(action="menu")
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import HealthCb, BotCb, BmCb, AccCb, WarmupCb, CleanerCb, ProxyCb, TaskCb, InfraCb
from bot.utils.op_helpers import safe_edit

log = logging.getLogger(__name__)
router = Router()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=HealthCb(action="menu"))
    return kb


# ── Sparklines & Visualization ────────────────────────────────────────────────────

# Unicode block elements for sparklines (8 levels)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _make_sparkline(values: list[float], width: int = 12) -> str:
    """Render a sparkline from a list of numeric values.

    Uses Unicode block elements scaled to 8 levels.
    Returns empty string for empty input.
    """
    if not values:
        return ""

    vmin = min(values)
    vmax = max(values)
    span = vmax - vmin

    if span < 0.001:
        # All values equal — flat line
        return "▄" * min(width, len(values))

    # Optionally compress if more values than width
    if len(values) > width:
        step = len(values) / width
        compressed = [values[int(i * step)] for i in range(width)]
    else:
        compressed = values

    chars = []
    for v in compressed:
        level = int((v - vmin) / span * 7)
        chars.append(_SPARK_CHARS[min(7, max(0, level))])

    return "".join(chars)


def _make_bar(value: float, max_val: float = 100.0, width: int = 10) -> str:
    """Render a horizontal bar. Returns string like '████░░░░░░'."""
    if max_val <= 0:
        return "█" * width
    filled = max(0, min(width, round(value / max_val * width)))
    return "█" * filled + "░" * (width - filled)


def _make_comparison_chart(
    accounts: list[dict], title: str, metric_key: str, max_val: float = 100.0
) -> str:
    """Side-by-side comparison chart for multiple accounts.

    Args:
        accounts: list of dicts with 'label', metric_key, and optional 'trend'
        title: chart title
        metric_key: key for the metric to compare
        max_val: maximum value for bar scaling (default 100)
    """
    if not accounts:
        return title + "\n<i>Нет данных</i>"

    lines = [title, ""]
    max_label = max(len(a.get("label", "?")) for a in accounts)

    for a in accounts[:12]:
        label = a.get("label", "?")
        val = float(a.get(metric_key, 0) or 0)
        bar = _make_bar(val, max_val, 12)
        trend = a.get("trend", "")
        lines.append(f"{trend} <code>{label:<{max_label}}</code> {bar} {val:.0f}")

    return "\n".join(lines)


async def _fetch_account_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN is_active THEN 1 END) AS active,
            COUNT(CASE WHEN cooldown_until > now() THEN 1 END) AS in_cooldown,
            ROUND(AVG(COALESCE(trust_score, 1.0))::numeric, 2) AS avg_trust,
            COUNT(CASE WHEN trust_score < 0.3 AND is_active THEN 1 END) AS critical,
            COUNT(CASE WHEN trust_score >= 0.3 AND trust_score < 0.6 AND is_active THEN 1 END) AS low_trust
        FROM tg_accounts
        WHERE owner_id=$1
        """,
        owner_id,
    )
    result = dict(row) if row else {"total": 0, "active": 0, "in_cooldown": 0, "avg_trust": 0,
                                     "critical": 0, "low_trust": 0}
    # Попробуем получить средний health_score из истории (24ч и вчера для тренда)
    try:
        hrow = await pool.fetchrow(
            """SELECT
                   ROUND(AVG(h.health_score) FILTER (
                       WHERE h.recorded_at > now() - INTERVAL '24 hours'
                   )::numeric, 1) AS avg_health,
                   ROUND(AVG(h.health_score) FILTER (
                       WHERE h.recorded_at > now() - INTERVAL '48 hours'
                         AND h.recorded_at <= now() - INTERVAL '24 hours'
                   )::numeric, 1) AS avg_health_yesterday
               FROM account_health_history h
               JOIN tg_accounts a ON a.id = h.account_id
               WHERE a.owner_id=$1
                 AND h.recorded_at > now() - INTERVAL '48 hours'""",
            owner_id,
        )
        result["avg_health"] = float(hrow["avg_health"] or 0) if hrow else 0.0
        result["avg_health_yesterday"] = (
            float(hrow["avg_health_yesterday"]) if hrow and hrow["avg_health_yesterday"] is not None else None
        )
    except Exception:
        result["avg_health"] = 0.0
        result["avg_health_yesterday"] = None
    return result


def _human_cooldown(cooldown_until: datetime, now: datetime) -> str:
    """Вернуть читаемое время до конца кулдауна (например 'через 2ч 15м')."""
    diff = cooldown_until.replace(tzinfo=timezone.utc) - now
    total_secs = max(0, int(diff.total_seconds()))
    if total_secs <= 0:
        return "скоро"
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    if hours >= 24:
        days = hours // 24
        rem_h = hours % 24
        if rem_h:
            return f"через {days}д {rem_h}ч"
        return f"через {days}д"
    if hours:
        return f"через {hours}ч {minutes}м"
    return f"через {minutes}м"


async def _fetch_flood_events_7d(pool: asyncpg.Pool, owner_id: int) -> int:
    try:
        val = await pool.fetchval(
            """
            SELECT COUNT(*) FROM account_flood_log afl
            JOIN tg_accounts ta ON ta.id = afl.account_id
            WHERE ta.owner_id=$1 AND afl.created_at > now() - interval '7 days'
            """,
            owner_id,
        )
        return int(val or 0)
    except Exception:
        return 0


# ── Menu ───────────────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "menu"))
async def cb_health_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    stats = await _fetch_account_stats(pool, user_id)
    flood_7d = await _fetch_flood_events_7d(pool, user_id)

    # Health score bar + trend vs yesterday
    avg_health = stats.get("avg_health", 0.0)
    avg_health_yesterday = stats.get("avg_health_yesterday")
    health_bar = "█" * int(avg_health / 10) + "░" * (10 - int(avg_health / 10)) if avg_health else "░" * 10
    if avg_health_yesterday is not None:
        delta = avg_health - avg_health_yesterday
        if delta >= 2:
            health_trend = f" ↑ +{delta:.0f} vs вчера"
        elif delta <= -2:
            health_trend = f" ↓ {delta:.0f} vs вчера"
        else:
            health_trend = " → без изменений"
    else:
        health_trend = ""

    # Infrastructure Pressure Score
    from services import infra_pressure
    pressure_data = await infra_pressure.compute_pressure(pool, user_id)
    p_score = pressure_data.get("score", 0)
    p_emoji = pressure_data.get("level_emoji", "🟢")
    p_label = pressure_data.get("level_label", "Норма")
    p_bar_filled = round(p_score / 10)
    p_bar = "█" * p_bar_filled + "░" * (10 - p_bar_filled)
    # Рекомендация при высоком давлении
    pressure_tip = ""
    if p_score > 70:
        pressure_tip = "\n💡 <i>Добавьте аккаунты или снизьте нагрузку</i>"

    # Pool and tag diversity
    from database import db as _db
    try:
        distinct_pools = await _db.get_distinct_pools(pool, user_id)
        pool_count = len(distinct_pools)
    except Exception:
        pool_count = 0
    try:
        distinct_tags = await _db.get_distinct_tags(pool, user_id)
        tag_count = len(distinct_tags)
    except Exception:
        tag_count = 0

    # Top-3 accounts by trust score
    top3_line = ""
    try:
        top3_rows = await pool.fetch(
            """SELECT first_name, username, phone, trust_score
               FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE AND trust_score IS NOT NULL
               ORDER BY trust_score DESC NULLS LAST LIMIT 3""",
            user_id,
        )
        if top3_rows:
            def _acc_short(r) -> str:
                return html.escape(
                    r["username"] or r["first_name"] or (r["phone"] or "")[-4:] or "?"
                )
            labels = ", ".join(
                f"{_acc_short(r)} ({float(r['trust_score']):.2f})" for r in top3_rows
            )
            top3_line = f"\n📊 Топ по надёжности: {labels}"
    except Exception:
        pass

    # Cooldown and problem accounts summary
    extra_alerts: list[str] = []
    try:
        cooldown_cnt = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND cooldown_until > now()",
            user_id,
        )
        if (cooldown_cnt or 0) > 0:
            extra_alerts.append(f"⚠️ {cooldown_cnt} аккаунт{'а' if cooldown_cnt < 5 else 'ов'} на кулдауне")
    except Exception:
        pass
    try:
        problem_cnt = await pool.fetchval(
            """SELECT COUNT(*) FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND COALESCE(acc_status,'active') IN ('banned','spamblock')""",
            user_id,
        )
        if (problem_cnt or 0) > 0:
            extra_alerts.append(f"🚨 {problem_cnt} проблемных аккаунта")
    except Exception:
        pass

    # Last restriction events (top-3) with severity icons
    restriction_lines: list[str] = []
    try:
        re_rows = await pool.fetch(
            """SELECT severity, event_type, created_at
               FROM restriction_events WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 3""",
            user_id,
        )
        _sev_icons = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}
        for re_row in re_rows:
            sev = re_row.get("severity") or "info"
            sev_icon = _sev_icons.get(sev, "🔔")
            etype = html.escape(re_row.get("event_type") or "event")
            dt_str = re_row["created_at"].strftime("%d.%m %H:%M") if re_row.get("created_at") else ""
            restriction_lines.append(f"  {sev_icon} <code>{dt_str}</code> {etype}")
    except Exception:
        pass

    text = (
        "❤️ <b>Здоровье инфраструктуры</b>\n\n"
        f"🩺 Состояние: <b>{avg_health:.0f}</b>/100  [{health_bar}]{health_trend}\n"
        f"🌡 Давление: {p_emoji} <b>{p_score}/100</b> — {p_label}  [{p_bar}]{pressure_tip}\n"
        f"🏊 Пулов: <b>{pool_count}</b> | 🏷 Тегов: <b>{tag_count}</b>\n"
        f"📱 Аккаунтов: <b>{stats['total']}</b> (активных: <b>{stats['active']}</b>)\n"
        f"⭐ Средняя надёжность: <b>{stats['avg_trust']}</b>\n"
        f"⏸ На паузе: <b>{stats['in_cooldown']}</b>"
        + top3_line
    )
    if stats["critical"] or stats["low_trust"]:
        alerts = []
        if stats["critical"]:
            alerts.append(f"🔴 Критическая надёжность: <b>{stats['critical']}</b>")
        if stats["low_trust"]:
            alerts.append(f"🟡 Низкая надёжность: <b>{stats['low_trust']}</b>")
        text += "\n\n" + " | ".join(alerts)
    if extra_alerts:
        text += "\n" + "  ".join(extra_alerts)
    text += f"\n📋 Блокировок за 7д: <b>{flood_7d}</b>"

    if restriction_lines:
        text += "\n\n🔔 <b>Последние события:</b>\n" + "\n".join(restriction_lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Аккаунты",       callback_data=HealthCb(action="accounts"))
    kb.button(text="🤖 Боты",           callback_data=HealthCb(action="bots_health"))
    kb.button(text="🔍 Реальная проверка", callback_data=HealthCb(action="real_check"))
    kb.button(text="📈 Тренд надёжности", callback_data=HealthCb(action="trust_trend"))
    kb.button(text="📊 Тренд здоровья",  callback_data=HealthCb(action="health_trend"))
    kb.button(text="📉 Графики здоровья", callback_data=HealthCb(action="sparklines"))
    kb.button(text="📊 Сравнить все",    callback_data=HealthCb(action="compare"))
    kb.button(text="🌊 История блокировок", callback_data=HealthCb(action="flood_log"))
    kb.button(text="💡 Рекомендации",   callback_data=HealthCb(action="recommendations"))
    kb.button(text="🌡 Давление",       callback_data=HealthCb(action="pressure"))
    kb.button(text="🎯 Советник",       callback_data=HealthCb(action="advisor"))
    kb.button(text="🔄 Авто-балансировка", callback_data=InfraCb(action="rebalance_preview"))
    kb.button(text="📥 Экспорт CSV",    callback_data=HealthCb(action="export_csv"))
    # ── Секция Действия ──
    kb.button(text="🔄 Переподключить аккаунты", callback_data=HealthCb(action="reconnect_menu"))
    kb.button(text="📊 Детальный лог",   callback_data=HealthCb(action="flood_log"))
    kb.button(text="⚠️ Кулдаун вручную", callback_data=HealthCb(action="set_cooldown_menu"))
    kb.button(text="🔄 Обновить",       callback_data=HealthCb(action="menu"))
    kb.button(text="◀️ Назад",          callback_data=BmCb(action="monitoring"))
    kb.adjust(2, 2, 2, 2, 2, 2, 3, 1, 1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Accounts health ────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "accounts"))
async def cb_health_accounts(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    rows = await pool.fetch(
        """
        SELECT a.id, a.phone, a.first_name, a.username, a.trust_score, a.cooldown_until,
               COALESCE(a.flood_count_7d, 0) AS flood_count_7d, a.is_active,
               a.pool, a.tags,
               (SELECT ROUND(h.health_score::numeric, 1)
                FROM account_health_history h
                WHERE h.account_id = a.id
                ORDER BY h.recorded_at DESC LIMIT 1) AS health_score
        FROM tg_accounts a
        WHERE a.owner_id=$1
        ORDER BY a.trust_score DESC NULLS LAST
        """,
        user_id,
    )

    lines = ["📱 <b>Здоровье аккаунтов</b>\n"]
    if not rows:
        lines.append("Нет подключённых аккаунтов.")
    else:
        now = datetime.now(timezone.utc)
        for acc in rows:
            trust = float(acc["trust_score"] or 1.0)
            health = float(acc["health_score"] or 0) if acc["health_score"] is not None else None
            phone = acc["phone"] or ""
            name = acc["username"] or acc["first_name"] or phone or f"id{acc['id']}"
            flood_until = acc["cooldown_until"]
            flood_cnt = int(acc["flood_count_7d"] or 0)

            hs_str = f" | health: {health:.0f}" if health is not None else ""

            # Pool and tags info
            pool_str = f" | 🏊 {acc['pool']}" if acc.get("pool") else ""
            tags_list = acc.get("tags") or []
            tags_str = f" | 🏷 {', '.join(tags_list[:3])}" if tags_list else ""

            if flood_until and flood_until.replace(tzinfo=timezone.utc) > now:
                human_cd = _human_cooldown(flood_until, now)
                time_str = flood_until.strftime("%d.%m %H:%M")
                lines.append(f"⏸ @{name} ({phone}) | <b>Пауза до {time_str}</b> ({human_cd}){hs_str}{pool_str}{tags_str}")
            elif trust < 0.5:
                lines.append(
                    f"⚠️ @{name} ({phone}) | надёжность: <b>{trust:.2f}</b> | блокировок: {flood_cnt}{hs_str}{pool_str}{tags_str}"
                )
            else:
                lines.append(
                    f"✅ @{name} ({phone}) | надёжность: <b>{trust:.2f}</b> | блокировок: {flood_cnt}{hs_str}{pool_str}{tags_str}"
                )

    kb = _back_kb()
    kb.adjust(1)
    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


# ── Real Telegram health check ─────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "real_check"))
async def cb_health_real_check(
    callback: CallbackQuery, pool: asyncpg.Pool,
) -> None:
    """Run check_account_status_full() for all accounts — actual Telegram verification."""
    await callback.answer()
    user_id = callback.from_user.id

    accounts = await pool.fetch(
        "SELECT id, session_str, phone, first_name, username, trust_score, device_model, "
        "system_version, app_version, proxy_id FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY id",
        user_id,
    )
    if not accounts:
        await safe_edit(callback, "📱 Нет активных аккаунтов для проверки.", reply_markup=_back_kb().as_markup())
        return

    await safe_edit(callback, f"🔍 <b>Проверяю {len(accounts)} аккаунтов через Telegram…</b>\n\nЭто может занять 30-60 секунд.", reply_markup=None)

    from services.account_manager import check_account_status_full
    from services.logger import log_exc_swallow

    status_label = {
        "active": "активен", "spamblock": "спам-ограничения", "cooldown": "пауза",
        "banned": "заблокирован Telegram", "deactivated": "удалён", "session_expired": "сессия истекла", "error": "ошибка"
    }
    status_emoji = {"active": "✅", "spamblock": "🚫", "cooldown": "⏸",
                    "banned": "⛔", "deactivated": "🗑", "session_expired": "🔑", "error": "⚠️"}

    results = []
    for acc in accounts:
        name = acc["username"] or acc["first_name"] or acc["phone"] or f"id{acc['id']}"
        try:
            res = await asyncio.wait_for(
                check_account_status_full(acc["session_str"], dict(acc), check_spambot=True),
                timeout=30.0,
            )
            status = res.get("status", "error")
            reason = res.get("reason", "")
        except asyncio.TimeoutError:
            status = "error"
            reason = "Таймаут"
        except Exception as e:
            log_exc_swallow(log, f"real_check failed acc={acc['id']}")
            status = "error"
            reason = str(e)[:60]

        # Update acc_status in DB
        try:
            await pool.execute(
                "UPDATE tg_accounts SET acc_status=$1, last_real_check_at=now(), real_check_status=$1 WHERE id=$2",
                status, acc["id"],
            )
            if status == "spamblock":
                await pool.execute(
                    "UPDATE tg_accounts SET trust_score=LEAST(trust_score, 0.3) WHERE id=$1",
                    acc["id"],
                )
        except Exception:
            log_exc_swallow(log, f"real_check: DB update failed acc={acc['id']}")

        emoji = status_emoji.get(status, "❓")
        label = status_label.get(status, status)
        reason_part = f": {reason[:50]}" if status not in ("active",) and reason else ""
        extra = f" — {label}{reason_part}" if status != "active" else ""
        results.append(f"{emoji} <b>{name}</b>{extra}")

    active_count = sum(1 for r in results if r.startswith("✅"))
    problem_count = len(results) - active_count

    text = (
        f"🔍 <b>Реальная проверка завершена</b>\n\n"
        f"Аккаунтов: {len(results)} | ✅ OK: {active_count} | ⚠️ Проблем: {problem_count}\n\n"
        + "\n".join(results[:20])
        + (f"\n…и ещё {len(results) - 20}" if len(results) > 20 else "")
    )

    kb = _back_kb()
    kb.button(text="📱 Обновить список", callback_data=HealthCb(action="accounts"))
    kb.adjust(1)
    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Bots health ────────────────────────────────────────────────────────────────

_http_session: aiohttp.ClientSession | None = None


async def _check_bot_alive(token: str, http_session: aiohttp.ClientSession | None = None) -> tuple[bool, str]:
    """Check if a bot token is valid via Bot API /getMe.

    Uses the shared http_session to avoid connection leaks.
    """
    import aiohttp
    url = f"https://api.telegram.org/bot{token}/getMe"
    session = http_session or _http_session
    if session is None:
        session = aiohttp.ClientSession()
        _http_session = session
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            data = await resp.json()
            if data.get("ok"):
                username = data["result"].get("username", "")
                return True, username
            return False, ""
    except Exception:
        return False, ""


@router.callback_query(HealthCb.filter(F.action == "bots_health"))
async def cb_health_bots(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    bots = await pool.fetch(
        """
        SELECT b.bot_id, b.username, b.first_name, b.token,
               COALESCE(aud.cnt, 0) AS user_count
        FROM managed_bots b
        LEFT JOIN (
            SELECT bot_id, COUNT(*) AS cnt
            FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
        ) aud ON aud.bot_id = b.bot_id
        WHERE b.added_by=$1 AND b.is_active=TRUE
        ORDER BY b.added_at DESC
        """,
        user_id,
    )

    lines = ["🤖 <b>Здоровье ботов</b>\n"]
    if not bots:
        lines.append("Нет добавленных ботов.")
    else:
        tasks = [_check_bot_alive(b["token"]) for b in bots]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for bot_rec, result in zip(bots, results):
            name = bot_rec["username"] or bot_rec["first_name"] or f"id{bot_rec['bot_id']}"
            user_count = bot_rec["user_count"]
            if isinstance(result, Exception) or not result[0]:
                lines.append(f"❌ @{name} — токен недействителен")
            else:
                lines.append(
                    f"✅ @{name} — активен | {user_count:,} пользователей"
                )

    kb = _back_kb()
    kb.adjust(1)
    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


# ── Flood log ──────────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "flood_log"))
async def cb_flood_log(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT afl.operation, afl.flood_seconds, afl.created_at, ta.phone
            FROM account_flood_log afl
            JOIN tg_accounts ta ON ta.id = afl.account_id
            WHERE ta.owner_id=$1
            ORDER BY afl.created_at DESC
            LIMIT 15
            """,
            user_id,
        )
        table_available = True
    except Exception:
        rows = []
        table_available = False

    lines = ["🌊 <b>История блокировок</b>\n"]
    if not table_available:
        lines.append("ℹ️ Таблица блокировок ещё не создана. Данные появятся автоматически.")
    elif not rows:
        lines.append("Нет flood-событий за последнее время.")
    else:
        for row in rows:
            dt = row["created_at"].strftime("%m-%d %H:%M") if row["created_at"] else "—"
            phone = row["phone"] or "—"
            op = row["operation"] or "—"
            secs = row["flood_seconds"] or 0
            lines.append(f"<code>{dt}</code> | {phone} | {op} | ⏱ {secs}s")

    kb = _back_kb()
    kb.adjust(1)
    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


# ── Trust score trends ─────────────────────────────────────────────────────────


@router.callback_query(HealthCb.filter(F.action == "trust_trend"))
async def cb_trust_trend(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT ta.phone, ta.first_name, ta.username,
                   ROUND(AVG(h.trust_score)::numeric, 2) AS avg_7d,
                   ROUND(MIN(h.trust_score)::numeric, 2) AS min_7d,
                   ta.trust_score AS current_score
            FROM account_trust_history h
            JOIN tg_accounts ta ON ta.id = h.account_id
            WHERE ta.owner_id=$1
              AND h.recorded_at > now() - INTERVAL '7 days'
            GROUP BY ta.id, ta.phone, ta.first_name, ta.username, ta.trust_score
            ORDER BY ta.trust_score DESC
            """,
            user_id,
        )
        table_ok = True
    except Exception:
        rows = []
        table_ok = False

    lines = ["📈 <b>Тренд надёжности аккаунтов (7 дней)</b>\n"]
    if not table_ok:
        lines.append("ℹ️ История ещё накапливается. Данные появятся через 30 минут.")
    elif not rows:
        lines.append("Нет данных за последние 7 дней.")
    else:
        for r in rows:
            name = r["username"] or r["first_name"] or r["phone"] or "—"
            cur = float(r["current_score"] or 0)
            avg = float(r["avg_7d"] or 0)
            mn = float(r["min_7d"] or 0)
            trend = "↗️" if cur >= avg else ("↘️" if cur < avg * 0.9 else "→")
            # 10-segment bar scaled 0.0-1.0
            filled = min(10, round(cur * 10))
            bar = "█" * filled + "░" * (10 - filled)
            delta = cur - avg
            delta_str = f"+{delta:.2f}" if delta >= 0 else f"{delta:.2f}"
            lines.append(
                f"{trend} <b>{html.escape(name[:20])}</b>\n"
                f"   [{bar}] {cur:.2f}  <i>avg {avg:.2f}  Δ{delta_str}  min {mn:.2f}</i>"
            )

    kb = _back_kb()
    kb.adjust(1)
    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


# ── Health score trends ────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "health_trend"))
async def cb_health_trend(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Show health_score trends from account_health_history."""
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT a.phone, a.first_name, a.username,
                      ROUND(AVG(h.health_score)::numeric, 1) AS avg_health_7d,
                      ROUND(AVG(h.health_score) FILTER (
                          WHERE h.recorded_at > now() - INTERVAL '24 hours'
                      )::numeric, 1) AS avg_health_24h,
                      ROUND(MIN(h.health_score)::numeric, 1) AS min_health_7d,
                      a.trust_score AS current_trust
               FROM account_health_history h
               JOIN tg_accounts a ON a.id = h.account_id
               WHERE a.owner_id=$1
                 AND h.recorded_at > now() - INTERVAL '7 days'
               GROUP BY a.id, a.phone, a.first_name, a.username, a.trust_score
               ORDER BY avg_health_7d DESC""",
            user_id,
        )
        table_ok = True
    except Exception:
        rows = []
        table_ok = False

    lines = ["📊 <b>Тренд здоровья аккаунтов</b>\n"]
    if not table_ok:
        lines.append("ℹ️ История здоровья накапливается. Первые данные появятся в течение часа.")
    elif not rows:
        lines.append("Нет данных за последние 7 дней.\n"
                     "Система сохраняет снимки состояния каждый час.")
    else:
        for r in rows:
            name = r["username"] or r["first_name"] or r["phone"] or "—"
            h7 = float(r["avg_health_7d"] or 0)
            h24 = float(r["avg_health_24h"] or 0)
            hmin = float(r["min_health_7d"] or 0)
            trust = float(r["current_trust"] or 0)

            # Trend direction
            if h24 >= h7:
                trend = "↗️"
            elif h24 < h7 * 0.9:
                trend = "↘️"
            else:
                trend = "→"

            # Health bar (0-100)
            bar_len = int(h7 / 10)
            bar = "█" * bar_len + "░" * (10 - bar_len)

            # Status emoji
            if h7 >= 80:
                status = "✅"
            elif h7 >= 50:
                status = "⚠️"
            else:
                status = "🔴"

            lines.append(
                f"{status} {trend} @{name}  [{bar}] {h7:.0f}/100\n"
                f"   <i>24ч: {h24:.0f} | min: {hmin:.0f} | trust: {trust:.2f}</i>"
            )

    kb = _back_kb()
    kb.adjust(1)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n<i>... показаны первые аккаунты</i>"
    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Sparkline charts ─────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "sparklines"))
async def cb_health_sparklines(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Sparkline charts for health score trends over 14 days per account."""
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT a.phone, a.first_name, a.username,
                      d.day::date AS day,
                      COALESCE(ROUND(AVG(h.health_score)::numeric, 1), 0) AS avg_health
               FROM tg_accounts a
               CROSS JOIN LATERAL generate_series(
                   current_date - INTERVAL '14 days',
                   current_date,
                   INTERVAL '1 day'
               ) AS d(day)
               LEFT JOIN account_health_history h
                 ON h.account_id = a.id
                AND h.recorded_at >= d.day
                AND h.recorded_at < d.day + INTERVAL '1 day'
               WHERE a.owner_id = $1
                 AND a.is_active = TRUE
               GROUP BY a.id, a.phone, a.first_name, a.username, d.day
               ORDER BY a.id, d.day""",
            user_id,
        )
    except Exception:
        rows = []

    if not rows:
        kb = _back_kb()
        kb.adjust(1)
        await safe_edit(
            callback,
            "📉 <b>Sparkline — тренды здоровья</b>\n\n"
            "ℹ️ Данные ещё накапливаются. Зайдите позже.",
            reply_markup=kb.as_markup(),
        )
        return

    # Group by account
    accounts: dict[int, dict] = {}
    for r in rows:
        aid = r["phone"] or str(r.get("username", "")) or str(r.get("first_name", ""))
        # Use phone as key
        key = str(r["phone"]) if r["phone"] else str(id(r))
        if key not in accounts:
            name = r["username"] or r["first_name"] or r["phone"] or "?"
            accounts[key] = {
                "label": name,
                "values": [],
                "current": 0,
            }
        val = float(r["avg_health"] or 0)
        accounts[key]["values"].append(val)

    # Build lines
    lines = ["📉 <b>Графики здоровья за 14 дней</b>\n"]
    lines.append("<code>" + "·" * 14 + "</code>  ← каждый символ = 1 день\n")

    for key, data in sorted(accounts.items(), key=lambda x: sum(x[1]["values"]) / max(len(x[1]["values"]), 1), reverse=True):
        vals = data["values"]
        if vals:
            spark = _make_sparkline(vals, 14)
            avg = sum(vals) / len(vals)
            cur = vals[-1] if vals else 0
            trend = "↗️" if cur >= avg * 1.05 else ("↘️" if cur < avg * 0.95 else "→")
            lines.append(
                f"{trend} <b>{html.escape(data['label'][:15])}</b> "
                f"<code>{spark}</code> {cur:.0f}/100"
            )

    kb = _back_kb()
    kb.adjust(1)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n<i>... показаны первые аккаунты</i>"
    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Comparison chart ──────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "compare"))
async def cb_health_compare(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Side-by-side comparison chart of all accounts."""
    await callback.answer()
    user_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT a.phone, a.first_name, a.username,
                      COALESCE(
                          (SELECT ROUND(AVG(h2.health_score)::numeric, 0)
                           FROM account_health_history h2
                           WHERE h2.account_id = a.id
                             AND h2.recorded_at > now() - INTERVAL '7 days'),
                          0
                      ) AS health_7d,
                      COALESCE(
                          (SELECT ROUND(AVG(h2.health_score)::numeric, 0)
                           FROM account_health_history h2
                           WHERE h2.account_id = a.id
                             AND h2.recorded_at > now() - INTERVAL '24 hours'),
                          0
                      ) AS health_24h,
                      a.trust_score,
                      a.is_active
               FROM tg_accounts a
               WHERE a.owner_id = $1
               ORDER BY health_7d DESC""",
            user_id,
        )
    except Exception:
        rows = []

    if not rows:
        kb = _back_kb()
        kb.adjust(1)
        await safe_edit(
            callback,
            "📊 <b>Сравнение аккаунтов</b>\n\n"
            "ℹ️ Нет данных. Добавьте аккаунты через ⚙️ Мониторинг → 📱 Аккаунты.",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📊 <b>Сравнительная карта здоровья</b>\n"]
    lines.append(f"<code>{'Аккаунт':<16} {'Health':>7} {'Trust':>6} {'Статус'}</code>")

    max_health = max((float(r["health_7d"] or 0) for r in rows), default=100)
    max_health = max(max_health, 100)

    for r in rows:
        name = r["username"] or r["first_name"] or r["phone"] or "?"
        h7 = float(r["health_7d"] or 0)
        trust = float(r["trust_score"] or 0)
        active = r["is_active"]

        bar = _make_bar(h7, max_health, 8)
        status = "✅" if active else "⛔"
        h24 = float(r["health_24h"] or 0)
        trend_sym = "↗️" if h24 >= h7 else "↘️"

        lines.append(
            f"{status}{trend_sym} <code>{name:<14}</code> {bar} {h7:>4.0f}   "
            f"<code>{trust:.2f}</code>"
        )

    kb = _back_kb()
    kb.adjust(1)
    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n<i>... показаны первые аккаунты</i>"
    await safe_edit(callback, text, reply_markup=kb.as_markup())

@router.callback_query(HealthCb.filter(F.action == "recommendations"))
async def cb_health_recommendations(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    accounts = await pool.fetch(
        "SELECT id, phone, first_name, username, trust_score, cooldown_until, "
        "flood_count_7d, is_active, added_at "
        "FROM tg_accounts WHERE owner_id=$1 ORDER BY trust_score ASC NULLS LAST LIMIT 20",
        user_id,
    )
    # Fetch health scores from history
    try:
        health_rows = await pool.fetch(
            """SELECT h.account_id,
                      ROUND(h.health_score::numeric, 1) AS health_score,
                      h.warmup_state,
                      h.success_ops, h.fail_ops
               FROM account_health_history h
               WHERE h.recorded_at > now() - INTERVAL '24 hours'
                 AND h.account_id = ANY(
                     SELECT id FROM tg_accounts WHERE owner_id=$1
                 )""",
            user_id,
        )
        health_map: dict[int, dict] = {}
        for hr in health_rows:
            aid = hr["account_id"]
            if aid not in health_map or hr["health_score"] < health_map[aid].get("health_score", 100):
                health_map[aid] = {
                    "health_score": float(hr["health_score"] or 0),
                    "warmup_state": hr["warmup_state"] or "raw",
                    "success_ops": int(hr["success_ops"] or 0),
                    "fail_ops": int(hr["fail_ops"] or 0),
                }
    except Exception:
        health_map = {}

    try:
        flood_7d_total = await pool.fetchval(
            "SELECT COUNT(*) FROM account_flood_log afl "
            "JOIN tg_accounts ta ON ta.id=afl.account_id "
            "WHERE ta.owner_id=$1 AND afl.created_at > now() - interval '7 days'",
            user_id,
        ) or 0
    except Exception:
        flood_7d_total = 0

    recs: list[str] = []
    critical_count = 0
    health_tips: set[str] = set()

    for acc in accounts:
        trust = float(acc["trust_score"] or 1.0)
        name = acc["username"] or acc["first_name"] or acc["phone"] or f"id{acc['id']}"
        flood_cnt = int(acc["flood_count_7d"] or 0)
        in_cooldown = bool(acc["cooldown_until"] and acc["cooldown_until"].replace(tzinfo=timezone.utc) > now)
        hinfo = health_map.get(acc["id"], {})
        health_s = hinfo.get("health_score")
        warmup = hinfo.get("warmup_state", "raw")

        if not acc["is_active"]:
            recs.append(
                f"🔴 <b>{name}</b> — деактивирован.\n"
                "   ↳ Используйте 🔄 Релог для переподключения.\n"
                "   ↳ Если не помогает — создайте новый аккаунт."
            )
            critical_count += 1
            health_tips.add("relog")
        elif in_cooldown:
            until = acc["cooldown_until"].strftime("%H:%M %d.%m")
            remaining = acc["cooldown_until"].replace(tzinfo=timezone.utc) - now
            hours_left = max(0, remaining.total_seconds() / 3600)
            recs.append(
                f"🟠 <b>{name}</b> — кулдаун до {until} ({hours_left:.0f}ч).\n"
                "   ↳ Не запускайте операции через этот аккаунт.\n"
                "   ↳ Авто-ротация уже защищает этот аккаунт."
            )
            critical_count += 1
        elif trust < 0.15:
            recs.append(
                f"🔴 <b>{name}</b> — trust {trust:.2f} (экстренно).\n"
                "   ↳ НЕМЕДЛЕННО прекратите все операции.\n"
                "   ↳ Дайте отдых 72+ часов. Проверьте прокси.\n"
                "   ↳ Риск перманентного бана высокий."
            )
            critical_count += 1
            health_tips.add("proxy_check")
        elif trust < 0.3:
            hs_line = f"   ↳ Health score: {health_s:.0f}/100\n" if health_s else ""
            recs.append(
                f"🔴 <b>{name}</b> — trust {trust:.2f} (критично).\n"
                f"{hs_line}"
                "   ↳ Дайте отдохнуть 48-72ч без операций.\n"
                "   ↳ Проверьте вручную — возможен shadowban."
            )
            critical_count += 1
            health_tips.add("shadowban_check")
        elif trust < 0.6:
            hs_line = f"   ↳ Health score: {health_s:.0f}/100\n" if health_s else ""
            recs.append(
                f"🟡 <b>{name}</b> — trust {trust:.2f} (низкий).\n"
                f"{hs_line}"
                f"   ↳ Снизьте интенсивность на 50%.\n"
                f"   ↳ Только read-only операции 24ч.\n"
                f"   ↳ Warmup state: {warmup}"
            )
            health_tips.add("intensity_reduce")
        elif flood_cnt > 5:
            avg_trust = float(acc["trust_score"] or 0)
            recs.append(
                f"🟡 <b>{name}</b> — {flood_cnt} flood-событий за 7д.\n"
                "   ↳ Увеличьте задержки до 60-90s.\n"
                "   ↳ Используйте pacing_mode=safe.\n"
                "   ↳ Чередуйте с другими аккаунтами."
            )
            health_tips.add("pacing_safe")

    # Health-aware general recommendations
    general: list[str] = []
    if len(accounts) == 0:
        general.append("ℹ️ Нет подключённых аккаунтов.\n   Добавьте через ⚙️ Мониторинг → 📱 Аккаунты.")
    elif critical_count == 0 and not recs:
        general.append("✅ <b>Все аккаунты в норме</b> — проблем не обнаружено.")
        general.append("💪 Продолжайте соблюдать safe pacing и мониторинг.")

    if flood_7d_total > 20:
        general.append(
            f"⚠️ <b>Высокая flood-активность</b>: {flood_7d_total} событий за неделю.\n"
            "   ↳ Снизьте параллельность операций.\n"
            "   ↳ Используйте pacing_mode=safe (120-180s задержки).\n"
            "   ↳ Проверьте расписания на предмет пересечений."
        )

    active_count = sum(1 for a in accounts if a["is_active"])
    low_trust = sum(1 for a in accounts if float(a["trust_score"] or 1) < 0.5)
    if active_count > 0 and low_trust / active_count > 0.5:
        general.append(
            "🔴 <b>Критическая ситуация</b>: >50% аккаунтов с низким trust.\n"
            "   ↳ Приостановите ВСЕ bulk-операции на 48 часов.\n"
            "   ↳ Запустите 🔄 Авто-ротацию для защиты аккаунтов.\n"
            "   ↳ Проверьте прокси — возможно, они скомпрометированы."
        )

    lines = ["💡 <b>Рекомендации по здоровью аккаунтов</b>\n"]
    if general:
        lines.extend(general)
    if recs:
        if general:
            lines.append("")
        lines.extend(recs)

    # Health tips based on detected patterns
    if health_tips:
        tips_text = []
        if "relog" in health_tips:
            tips_text.append("💡 <b>Совет:</b> Используйте кнопку 🔄 Релог в списке аккаунтов для быстрого переподключения.")
        if "proxy_check" in health_tips:
            tips_text.append("💡 <b>Совет:</b> Проверьте прокси в ⚙️ Мониторинг → 🌐 Прокси. Скомпрометированные IP снижают trust.")
        if "shadowban_check" in health_tips:
            tips_text.append("💡 <b>Совет:</b> Откройте 📊 Аналитика → 🔔 Алерты — проверьте restriction events.")
        if "intensity_reduce" in health_tips:
            tips_text.append("💡 <b>Совет:</b> Используйте pacing_mode=safe в bulk-операциях для автоматических безопасных задержек.")
        if "pacing_safe" in health_tips:
            tips_text.append("💡 <b>Совет:</b> При создании каналов/групп выбирайте темп «Безопасный» для минимального риска.")
        if tips_text:
            lines.append("")
            lines.append("─" * 10)
            lines.extend(tips_text)

    text = "\n".join(lines)
    if len(text) > 3800:
        text = text[:3750] + "\n\n<i>... и другие аккаунты</i>"

    kb = _back_kb()
    kb.button(text="🔄 Авто-ротация", callback_data=HealthCb(action="auto_rotate_confirm"))
    kb.button(text="🔄 Обновить", callback_data=HealthCb(action="recommendations"))
    kb.adjust(1)
    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Auto-rotation ──────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "auto_rotate_confirm"))
async def cb_auto_rotate_confirm(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Show confirmation before auto-rotating unhealthy accounts."""
    await callback.answer()
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    critical = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND trust_score < 0.3",
        user_id,
    ) or 0
    low = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND trust_score >= 0.3 AND trust_score < 0.6",
        user_id,
    ) or 0
    cooldown = await pool.fetchval(
        "SELECT COUNT(*) FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND cooldown_until > now()",
        user_id,
    ) or 0

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить авто-ротацию", callback_data=HealthCb(action="auto_rotate"))
    kb.button(text="❌ Отмена", callback_data=HealthCb(action="recommendations"))
    kb.adjust(1)

    await safe_edit(
        callback,
        "🔄 <b>Авто-ротация аккаунтов</b>\n\n"
        "Система проверит все аккаунты и применит защитные меры:\n\n"
        f"🔴 Критически низкий trust (<0.3): <b>{critical}</b> акк.\n"
        "   → Поставить кулдаун 72 часа\n"
        f"🟡 Низкий trust (0.3–0.6): <b>{low}</b> акк.\n"
        "   → Поставить кулдаун 24 часа\n"
        f"🌊 Уже в кулдауне: <b>{cooldown}</b> акк.\n"
        "   → Пропустить\n\n"
        "⚠️ Аккаунты в кулдауне не будут использоваться для операций автоматически.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(HealthCb.filter(F.action == "auto_rotate"))
async def cb_auto_rotate(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Execute auto-rotation: apply cooldowns to low-trust accounts."""
    await callback.answer("⏳ Ротирую аккаунты...")
    user_id = callback.from_user.id
    from datetime import timedelta
    now = datetime.now(timezone.utc)

    # Critical: trust < 0.3 → 72h cooldown
    critical_updated = await pool.execute(
        "UPDATE tg_accounts SET cooldown_until = $1 "
        "WHERE owner_id=$2 AND is_active=TRUE AND trust_score < 0.3 "
        "AND (cooldown_until IS NULL OR cooldown_until < now())",
        now + timedelta(hours=72), user_id,
    )
    # Low: trust 0.3–0.6 → 24h cooldown
    low_updated = await pool.execute(
        "UPDATE tg_accounts SET cooldown_until = $1 "
        "WHERE owner_id=$2 AND is_active=TRUE AND trust_score >= 0.3 AND trust_score < 0.6 "
        "AND (cooldown_until IS NULL OR cooldown_until < now())",
        now + timedelta(hours=24), user_id,
    )

    def _count(pg_result: str) -> int:
        try:
            return int(pg_result.split()[-1])
        except Exception:
            return 0

    crit_n = _count(critical_updated)
    low_n = _count(low_updated)

    kb = InlineKeyboardBuilder()
    kb.button(text="❤️ К панели здоровья", callback_data=HealthCb(action="menu"))
    kb.adjust(1)
    await safe_edit(
        callback,
        "✅ <b>Авто-ротация выполнена</b>\n\n"
        f"🔴 Критических → 72ч кулдаун: <b>{crit_n}</b>\n"
        f"🟡 С низким trust → 24ч кулдаун: <b>{low_n}</b>\n\n"
        "Эти аккаунты не будут использоваться в операциях до окончания кулдауна.\n"
        "Trust score восстановится со временем при отсутствии операций.",
        reply_markup=kb.as_markup(),
    )


# ── CSV Export ─────────────────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "export_csv"))
async def cb_health_export_csv(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Export account health data as CSV file."""
    user_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    accounts = await pool.fetch(
        """SELECT phone, first_name, username, trust_score, is_active,
                  cooldown_until, flood_count_7d, device_model, added_at
           FROM tg_accounts WHERE owner_id=$1 ORDER BY trust_score DESC NULLS LAST""",
        user_id,
    )
    if not accounts:
        await callback.answer("Нет аккаунтов для экспорта", show_alert=True)
        return
    await callback.answer("⏳ Генерирую CSV...")

    try:
        flood_map: dict[str, int] = {}
        flood_rows = await pool.fetch(
            """SELECT ta.phone, COUNT(afl.id) AS cnt
               FROM account_flood_log afl
               JOIN tg_accounts ta ON ta.id = afl.account_id
               WHERE ta.owner_id=$1 AND afl.created_at > now() - interval '7 days'
               GROUP BY ta.phone""",
            user_id,
        )
        for r in flood_rows:
            flood_map[r["phone"] or ""] = int(r["cnt"])
    except Exception:
        flood_map = {}

    import csv
    import io
    from aiogram.types import BufferedInputFile

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "phone", "first_name", "username", "trust_score", "status",
        "cooldown_until", "flood_events_7d", "device_model", "added_at",
    ])
    for acc in accounts:
        cd = acc["cooldown_until"]
        in_cooldown = bool(cd and cd.replace(tzinfo=timezone.utc) > now)
        status = "cooldown" if in_cooldown else ("active" if acc["is_active"] else "inactive")
        writer.writerow([
            acc["phone"] or "",
            acc["first_name"] or "",
            acc["username"] or "",
            f"{float(acc['trust_score'] or 0):.2f}",
            status,
            cd.strftime("%Y-%m-%d %H:%M") if cd else "",
            flood_map.get(acc["phone"] or "", 0),
            acc["device_model"] or "",
            acc["added_at"].strftime("%Y-%m-%d") if acc.get("added_at") else "",
        ])

    data = buf.getvalue().encode("utf-8-sig")
    file = BufferedInputFile(data, filename="account_health.csv")
    await callback.message.answer_document(
        file,
        caption=(
            "📥 <b>Экспорт здоровья аккаунтов</b>\n"
            f"Всего: {len(accounts)} аккаунтов\n"
            "<i>trust_score, status, flood за 7 дней, device_model</i>"
        ),
        parse_mode="HTML",
    )


# ── Infrastructure Pressure Score ─────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "pressure"))
async def cb_pressure_score(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services import infra_pressure
    data = await infra_pressure.compute_pressure(pool, callback.from_user.id)
    report = infra_pressure.format_pressure_report(data)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=HealthCb(action="pressure"))
    kb.button(text="🎯 Советник", callback_data=HealthCb(action="advisor"))
    kb.button(text="◀️ Назад",   callback_data=HealthCb(action="menu"))
    kb.adjust(2, 1)
    await safe_edit(callback, report, reply_markup=kb.as_markup())


# ── Infrastructure Advisor ─────────────────────────────────────────────────────

_ADVISOR_ACTION_BUTTONS: dict[str, tuple[str, object]] = {
    "accounts": ("📱 Аккаунты",  AccCb(action="menu")),
    "warmup":   ("🌡 Разогрев",  WarmupCb(action="menu")),
    "cleaner":  ("🧹 Очистка",   CleanerCb(action="menu")),
    "proxies":  ("🌐 Прокси",    ProxyCb(action="menu")),
    "tasks":    ("⚡ Задачи",    TaskCb(action="list")),
}


@router.callback_query(HealthCb.filter(F.action == "advisor"))
async def cb_infra_advisor(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services import infra_advisor
    recs = await infra_advisor.get_recommendations(pool, callback.from_user.id)
    text = infra_advisor.format_recommendations(recs)
    kb = InlineKeyboardBuilder()
    # Collect unique actions from recommendations (up to 3)
    seen: set[str] = set()
    action_cbs = []
    for rec in recs:
        action = rec.get("action", "")
        if action and action in _ADVISOR_ACTION_BUTTONS and action not in seen:
            seen.add(action)
            label, cb_data = _ADVISOR_ACTION_BUTTONS[action]
            action_cbs.append((label, cb_data))
        if len(action_cbs) >= 3:
            break
    for label, cb_data in action_cbs:
        kb.button(text=label, callback_data=cb_data)
    if action_cbs:
        kb.adjust(min(len(action_cbs), 3))
    kb.button(text="🔄 Обновить", callback_data=HealthCb(action="advisor"))
    kb.button(text="🌡 Давление", callback_data=HealthCb(action="pressure"))
    kb.button(text="◀️ Назад",   callback_data=HealthCb(action="menu"))
    kb.adjust(*([min(len(action_cbs), 3)] if action_cbs else []), 2, 1)
    await safe_edit(callback, text, reply_markup=kb.as_markup())


# ── Actions: Reconnect menu ────────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "reconnect_menu"))
async def cb_reconnect_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Список аккаунтов с неактивными сессиями для переподключения."""
    await callback.answer()
    user_id = callback.from_user.id

    rows = await pool.fetch(
        """SELECT id, phone, first_name, username,
                  COALESCE(acc_status, 'active') AS acc_status, is_active
           FROM tg_accounts WHERE owner_id=$1
           ORDER BY is_active ASC, acc_status DESC""",
        user_id,
    )
    if not rows:
        kb = _back_kb()
        await safe_edit(callback, "📱 Нет подключённых аккаунтов.", reply_markup=kb.as_markup())
        return

    lines = ["🔄 <b>Переподключение аккаунтов</b>\n",
             "Выберите аккаунт для переподключения (релог):\n"]

    kb = InlineKeyboardBuilder()
    from bot.callbacks import AccCb
    for acc in rows:
        st = acc["acc_status"]
        active = acc["is_active"]
        name = acc.get("username") or acc.get("first_name") or acc.get("phone") or f"id{acc['id']}"
        status_icon = "✅" if active and st == "active" else ("🔑" if st == "session_expired" else "⚠️")
        kb.button(
            text=f"{status_icon} {html.escape(name[:20])} — {st}",
            callback_data=AccCb(action="relog", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=HealthCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


# ── Actions: Set cooldown menu ─────────────────────────────────────────────────

@router.callback_query(HealthCb.filter(F.action == "set_cooldown_menu"))
async def cb_set_cooldown_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Выбор аккаунта для ручной установки кулдауна."""
    await callback.answer()
    user_id = callback.from_user.id

    rows = await pool.fetch(
        """SELECT id, phone, first_name, username, cooldown_until, is_active
           FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE
           ORDER BY phone""",
        user_id,
    )
    if not rows:
        kb = _back_kb()
        await safe_edit(callback, "📱 Нет активных аккаунтов.", reply_markup=kb.as_markup())
        return

    now = datetime.now(timezone.utc)
    lines = ["⚠️ <b>Установить кулдаун</b>\n",
             "Выберите аккаунт — будет установлен кулдаун 24 часа:\n"]

    kb = InlineKeyboardBuilder()
    for acc in rows:
        name = acc.get("username") or acc.get("first_name") or acc.get("phone") or f"id{acc['id']}"
        cd_until = acc.get("cooldown_until")
        if cd_until:
            cd_aware = cd_until if cd_until.tzinfo else cd_until.replace(tzinfo=timezone.utc)
            if cd_aware > now:
                cd_label = f" [кулдаун {_human_cooldown(cd_aware, now)}]"
            else:
                cd_label = ""
        else:
            cd_label = ""
        kb.button(
            text=f"⏸ {html.escape(name[:20])}{cd_label}",
            callback_data=HealthCb(action="set_cooldown_confirm", page=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=HealthCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(HealthCb.filter(F.action == "set_cooldown_confirm"))
async def cb_set_cooldown_confirm(callback: CallbackQuery, callback_data: HealthCb, pool: asyncpg.Pool) -> None:
    """Устанавливает кулдаун 24ч на выбранный аккаунт."""
    await callback.answer()
    user_id = callback.from_user.id
    acc_id = callback_data.page  # используем page как acc_id

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cd_until = now + timedelta(hours=24)

    await pool.execute(
        "UPDATE tg_accounts SET cooldown_until=$1 WHERE id=$2 AND owner_id=$3",
        cd_until, acc_id, user_id,
    )

    acc = await pool.fetchrow("SELECT phone, first_name, username FROM tg_accounts WHERE id=$1", acc_id)
    name = "—"
    if acc:
        name = acc.get("username") or acc.get("first_name") or acc.get("phone") or f"id{acc_id}"

    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Ещё кулдаун", callback_data=HealthCb(action="set_cooldown_menu"))
    kb.button(text="◀️ К дашборду", callback_data=HealthCb(action="menu"))
    kb.adjust(1)

    await safe_edit(
        callback,
        f"✅ <b>Кулдаун установлен</b>\n\n"
        f"Аккаунт <b>{html.escape(name)}</b>\n"
        f"Кулдаун до: <b>{cd_until.strftime('%d.%m.%Y %H:%M')} UTC</b>\n"
        f"(через 24 часа)\n\n"
        "<i>Аккаунт не будет использоваться в операциях до снятия кулдауна.</i>",
        reply_markup=kb.as_markup(),
    )

