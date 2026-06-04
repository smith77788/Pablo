"""EPOCH VI: Infrastructure Health Center — единый центр самовосстановления.

Команда: /health_center  или через InfraHCCb(action="menu")
Показывает:
  — Общий health score 0-100 с трендом
  — Активные аномалии (anomaly_events)
  — Активные алерты инфраструктуры
  — История событий восстановления (recovery_events)
  — Ручной запуск Recovery Engine
  — Просмотр деталей аномалии / события
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import InfraHCCb
from bot.utils.op_helpers import safe_edit
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_SEVERITY_ICON = {"critical": "🔴", "warning": "🟡", "info": "🔵", "opportunity": "💡"}
_STATUS_ICON = {"success": "✅", "failed": "❌", "pending": "⏳", "running": "🔄", "skipped": "⏩"}
_ANOMALY_ICON = {
    "error_spike": "⚡",
    "success_drop": "📉",
    "queue_surge": "📦",
    "flood_wave": "🌊",
    "trust_collapse": "💥",
    "latency_spike": "🐢",
}
_RECOVERY_ICON = {
    "account": "👤",
    "proxy": "🔗",
    "session": "🔐",
    "queue": "📋",
    "operation": "⚙️",
}


def _score_bar(score: int) -> str:
    filled = round(score / 10)
    empty = 10 - filled
    if score >= 80:
        ch = "█"
    elif score >= 50:
        ch = "▓"
    else:
        ch = "░"
    return f"[{ch * filled}{'─' * empty}] {score}/100"


def _score_emoji(score: int) -> str:
    if score >= 80:
        return "🟢"
    elif score >= 60:
        return "🟡"
    elif score >= 40:
        return "🟠"
    return "🔴"


# ─── Команда /health_center ───────────────────────────────────────────────────

@router.message(Command("health_center"))
async def cmd_health_center(message: Message, pool: asyncpg.Pool) -> None:
    await _show_hc_menu(message, pool, message.from_user.id)


async def _show_hc_menu(
    target: Message | CallbackQuery,
    pool: asyncpg.Pool,
    owner_id: int,
) -> None:
    from services import recovery_engine, anomaly_detector

    health = await recovery_engine.get_current_health(pool, owner_id)
    score = health.get("health_score", 0)

    anomalies = await anomaly_detector.get_active_anomalies(pool, owner_id)
    critical_count = sum(1 for a in anomalies if a.get("severity") == "critical")
    warning_count = sum(1 for a in anomalies if a.get("severity") == "warning")

    try:
        alerts_row = await pool.fetchrow(
            "SELECT COUNT(*) AS cnt FROM infrastructure_alerts WHERE owner_id=$1 AND is_active=TRUE",
            owner_id,
        )
        active_alerts = (alerts_row["cnt"] if alerts_row else 0) or 0
    except Exception:
        active_alerts = 0

    try:
        recoveries_row = await pool.fetchrow(
            """SELECT COUNT(*) AS cnt FROM recovery_events
               WHERE owner_id=$1 AND created_at > NOW() - INTERVAL '24 hours'""",
            owner_id,
        )
        recent_recoveries = (recoveries_row["cnt"] if recoveries_row else 0) or 0
    except Exception:
        recent_recoveries = 0

    score_icon = _score_emoji(score)
    lines = [
        f"🏥 <b>Infrastructure Health Center</b>",
        f"",
        f"{score_icon} <b>Health Score: {_score_bar(score)}</b>",
        f"",
    ]

    if critical_count:
        lines.append(f"🔴 Критических аномалий: <b>{critical_count}</b>")
    if warning_count:
        lines.append(f"🟡 Предупреждений: <b>{warning_count}</b>")
    if active_alerts:
        lines.append(f"🚨 Активных алертов: <b>{active_alerts}</b>")
    if recent_recoveries:
        lines.append(f"🔄 Восстановлений за 24ч: <b>{recent_recoveries}</b>")

    if not critical_count and not warning_count and not active_alerts:
        lines.append("✅ <b>Инфраструктура в норме — аномалий нет</b>")

    if health.get("accounts_total") is not None:
        acc_ready = health.get("accounts_ready", 0)
        acc_total = health.get("accounts_total", 0)
        avg_trust = health.get("avg_trust_score", 0)
        lines.append(f"")
        lines.append(f"📱 Аккаунтов: <b>{acc_ready}/{acc_total}</b> готовы")
        lines.append(f"🛡 Trust: <b>{round(float(avg_trust or 0) * 100)}%</b>")
        ops_f = health.get("ops_failed_24h", 0) or 0
        ops_d = health.get("ops_done_24h", 0) or 0
        if ops_f + ops_d > 0:
            fail_rate = round(ops_f / (ops_f + ops_d) * 100)
            lines.append(f"⚙️ Операции 24ч: {ops_d} ✅ / {ops_f} ❌ ({fail_rate}% ошибок)")

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if anomalies:
        kb.button(text=f"⚡ Аномалии ({len(anomalies)})", callback_data=InfraHCCb(action="anomalies"))
    kb.button(text="📋 Восстановления", callback_data=InfraHCCb(action="recoveries"))
    kb.button(text="🔄 Запустить Recovery", callback_data=InfraHCCb(action="run_recovery"))
    kb.button(text="📊 Тренд здоровья", callback_data=InfraHCCb(action="health_trend"))
    kb.button(text="🔍 Copilot анализ", callback_data=InfraHCCb(action="copilot"))
    kb.button(text="◀️ Главное меню", callback_data=InfraHCCb(action="back"))
    kb.adjust(2, 2, 1, 1)

    if isinstance(target, CallbackQuery):
        await safe_edit(target, text, reply_markup=kb.as_markup())
    else:
        await target.answer(text, reply_markup=kb.as_markup())


# ─── Callbacks ────────────────────────────────────────────────────────────────

@router.callback_query(InfraHCCb.filter(F.action == "menu"))
async def cb_hc_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await _show_hc_menu(callback, pool, callback.from_user.id)


@router.callback_query(InfraHCCb.filter(F.action == "anomalies"))
async def cb_hc_anomalies(
    callback: CallbackQuery,
    callback_data: InfraHCCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    page = callback_data.page

    from services import anomaly_detector
    all_anomalies = await anomaly_detector.get_active_anomalies(pool, owner_id)

    per_page = 5
    total = len(all_anomalies)
    start = page * per_page
    anomalies = all_anomalies[start: start + per_page]

    if not anomalies:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
        await safe_edit(callback, "✅ Активных аномалий нет.", reply_markup=kb.as_markup())
        return

    lines = [f"⚡ <b>Активные аномалии</b> ({total})\n"]
    for a in anomalies:
        icon = _ANOMALY_ICON.get(a.get("anomaly_type", ""), "⚠️")
        sev = _SEVERITY_ICON.get(a.get("severity", "warning"), "⚠️")
        title = html.escape(a.get("title", ""))
        desc = html.escape((a.get("description") or "")[:120])
        detected = ""
        if a.get("detected_at"):
            detected = a["detected_at"].strftime("%d.%m %H:%M") if hasattr(a["detected_at"], "strftime") else ""
        lines.append(
            f"{sev}{icon} <b>{title}</b>\n"
            f"   {desc}\n"
            f"   <i>{detected}</i>\n"
        )

    kb = InlineKeyboardBuilder()
    for a in anomalies:
        anom_id = a.get("id", 0)
        kb.button(
            text=f"✅ Разрешить #{anom_id}",
            callback_data=InfraHCCb(action="resolve_anomaly", item_id=anom_id),
        )
    if page > 0:
        kb.button(text="◀️ Пред.", callback_data=InfraHCCb(action="anomalies", page=page - 1))
    if start + per_page < total:
        kb.button(text="След. ▶️", callback_data=InfraHCCb(action="anomalies", page=page + 1))
    kb.button(text="🔄 Recovery Engine", callback_data=InfraHCCb(action="run_recovery"))
    kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(InfraHCCb.filter(F.action == "resolve_anomaly"))
async def cb_hc_resolve_anomaly(
    callback: CallbackQuery,
    callback_data: InfraHCCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    anom_id = callback_data.item_id

    from services import anomaly_detector
    ok = await anomaly_detector.resolve_anomaly(pool, anom_id, owner_id)

    if ok:
        await callback.answer("✅ Аномалия разрешена", show_alert=False)
    await _show_hc_menu(callback, pool, owner_id)


@router.callback_query(InfraHCCb.filter(F.action == "recoveries"))
async def cb_hc_recoveries(
    callback: CallbackQuery,
    callback_data: InfraHCCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    page = callback_data.page

    from services import recovery_engine
    all_events = await recovery_engine.get_recent_recovery_events(pool, owner_id, limit=50)

    per_page = 6
    total = len(all_events)
    start = page * per_page
    events = all_events[start: start + per_page]

    if not events:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
        await safe_edit(
            callback,
            "📋 <b>Восстановления</b>\n\nСобытий восстановления не найдено.",
            reply_markup=kb.as_markup(),
        )
        return

    lines = [f"📋 <b>События восстановления</b> ({total})\n"]
    for ev in events:
        rtype = ev.get("recovery_type", "")
        icon = _RECOVERY_ICON.get(rtype, "⚙️")
        status_icon = _STATUS_ICON.get(ev.get("status", ""), "•")
        action = ev.get("action", "")
        target_id = ev.get("target_id")
        target_label = f"#{target_id}" if target_id else ""
        created = ""
        if ev.get("created_at"):
            created = ev["created_at"].strftime("%d.%m %H:%M") if hasattr(ev["created_at"], "strftime") else ""

        details = ev.get("details") or {}
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except Exception:
                details = {}
        label = details.get("label", target_label)

        lines.append(
            f"{status_icon}{icon} <b>{rtype}</b> → {action}"
            f"{f' ({html.escape(str(label))})' if label else ''}"
            f" <i>{created}</i>"
        )

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="◀️ Пред.", callback_data=InfraHCCb(action="recoveries", page=page - 1))
    if start + per_page < total:
        kb.button(text="След. ▶️", callback_data=InfraHCCb(action="recoveries", page=page + 1))
    kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
    kb.adjust(2, 1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(InfraHCCb.filter(F.action == "run_recovery"))
async def cb_hc_run_recovery(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("🔄 Запуск Recovery Engine...", show_alert=False)
    owner_id = callback.from_user.id

    try:
        from services import recovery_engine
        import asyncio

        # Запустить recovery engine для этого пользователя
        actions = await recovery_engine._recover_owner(pool, None, owner_id)
        success = [a for a in actions if a.status == "success"]
        skipped = [a for a in actions if a.status == "skipped"]

        lines = ["🔄 <b>Recovery Engine — результаты</b>\n"]
        if not actions:
            lines.append("✅ Проблем не обнаружено — инфраструктура в норме.")
        else:
            for a in actions[:8]:
                icon = _RECOVERY_ICON.get(a.recovery_type, "⚙️")
                status_icon = _STATUS_ICON.get(a.status, "•")
                sev_icon = _SEVERITY_ICON.get(a.severity, "⚠️")
                label = a.details.get("label", f"#{a.target_id}")
                lines.append(
                    f"{status_icon}{icon} {sev_icon} <b>{a.recovery_type}</b> → {a.action}"
                    f" ({html.escape(str(label))})"
                )
            if len(actions) > 8:
                lines.append(f"<i>...и ещё {len(actions) - 8} действий</i>")

            lines.append(
                f"\n<b>Итого:</b> {len(actions)} действий, "
                f"{len(success)} успешных, {len(skipped)} пропущено."
            )

        kb = InlineKeyboardBuilder()
        kb.button(text="📋 История", callback_data=InfraHCCb(action="recoveries"))
        kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
        kb.adjust(1)

        await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())
    except Exception as e:
        log_exc_swallow(log, "cb_hc_run_recovery")
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
        await safe_edit(
            callback,
            f"❌ Ошибка при запуске Recovery Engine: {html.escape(str(e)[:100])}",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(InfraHCCb.filter(F.action == "health_trend"))
async def cb_hc_health_trend(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    try:
        rows = await pool.fetch(
            """SELECT health_score, snapshot_at
               FROM system_health_snapshots
               WHERE owner_id=$1
               ORDER BY snapshot_at DESC
               LIMIT 24""",
            owner_id,
        )
        rows = list(reversed(rows))
    except Exception:
        rows = []

    lines = ["📊 <b>Тренд здоровья системы (24ч)</b>\n"]

    if not rows:
        lines.append("<i>Данных пока нет — подождите первый цикл Recovery Engine (~15 мин)</i>")
    else:
        scores = [r["health_score"] for r in rows]
        current = scores[-1]
        avg = round(sum(scores) / len(scores))
        min_score = min(scores)
        max_score = max(scores)

        # Спарклайн
        _SPARK = "▁▂▃▄▅▆▇█"
        spark = ""
        for s in scores[-16:]:
            idx = min(7, int(s / 12.5))
            spark += _SPARK[idx]

        lines.append(f"{_score_emoji(current)} Текущий: <b>{current}/100</b>")
        lines.append(f"📈 Максимум: <b>{max_score}</b> | Минимум: <b>{min_score}</b> | Среднее: <b>{avg}</b>")
        lines.append(f"<code>{spark}</code>")
        lines.append("")

        # Последние 6 снапшотов
        for r in rows[-6:]:
            ts = r["snapshot_at"].strftime("%H:%M") if hasattr(r["snapshot_at"], "strftime") else ""
            sc = r["health_score"]
            lines.append(f"{_score_emoji(sc)} {ts}  {_score_bar(sc)}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=InfraHCCb(action="health_trend"))
    kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(InfraHCCb.filter(F.action == "copilot"))
async def cb_hc_copilot(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer("🤖 Анализирую...", show_alert=False)
    owner_id = callback.from_user.id

    try:
        from services import infra_copilot
        insights = await infra_copilot.run_full_analysis(pool, owner_id)
        text = infra_copilot.format_copilot_report(insights, max_items=6)
    except Exception as e:
        text = f"❌ Ошибка Copilot: {html.escape(str(e)[:150])}"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=InfraHCCb(action="copilot"))
    kb.button(text="◀️ Назад", callback_data=InfraHCCb(action="menu"))
    kb.adjust(1)

    await safe_edit(callback, text, reply_markup=kb.as_markup())


@router.callback_query(InfraHCCb.filter(F.action == "back"))
async def cb_hc_back(callback: CallbackQuery) -> None:
    await callback.answer()
    from bot.callbacks import BmCb
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 BotMother OS", callback_data=BmCb(action="menu"))
    await safe_edit(callback, "Выберите раздел:", reply_markup=kb.as_markup())
