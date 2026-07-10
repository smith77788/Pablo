"""Analytics Dashboard — статистика и метрики для дашборда владельца.

Предоставляет агрегированную статистику по аккаунтам, операциям,
аудитории и доходам для отображения в мини-приложении и уведомлениях.

Использование:
    from services.analytics_dashboard import (
        get_dashboard_stats,
        get_realtime_metrics,
        get_historical_data,
        export_analytics,
    )
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import asyncpg

from services.logger import log_exc_swallow, timed

log = logging.getLogger(__name__)


async def get_dashboard_stats(pool: asyncpg.Pool, owner_id: int) -> dict[str, Any]:
    """Общая статистика дашборда — сводка по всем ресурсам владельца.

    Возвращает:
        - accounts: total, active, banned, spamblock
        - operations: total, success, failed, running (за 24ч)
        - audience: total_users, new_24h, new_7d
        - health: avg_trust, accounts_at_risk
        - revenue: estimated_usd (если есть payment данные)
    """
    with timed(log, "get_dashboard_stats", extra={"owner_id": owner_id}):
        queries = [
            # Accounts summary
            (
                """SELECT
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_active = TRUE) AS active,
                       COUNT(*) FILTER (WHERE acc_status = 'banned') AS banned,
                       COUNT(*) FILTER (WHERE acc_status = 'spamblock') AS spamblock
                   FROM tg_accounts
                   WHERE owner_id = $1""",
                (owner_id,),
            ),
            # Operations last 24h
            (
                """SELECT
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE result = 'success') AS success,
                       COUNT(*) FILTER (WHERE result = 'failed') AS failed
                   FROM operation_audit
                   WHERE owner_id = $1
                     AND occurred_at > NOW() - INTERVAL '24 hours'""",
                (owner_id,),
            ),
            # Running operations
            (
                """SELECT COUNT(*) AS running
                   FROM operation_queue
                   WHERE owner_id = $1
                     AND status = 'running'""",
                (owner_id,),
            ),
            # Audience (all managed bots of owner)
            (
                """SELECT
                       COUNT(DISTINCT bu.user_id) AS total_users,
                       COUNT(DISTINCT bu.user_id) FILTER (
                           WHERE bu.first_seen > NOW() - INTERVAL '24 hours'
                       ) AS new_24h,
                       COUNT(DISTINCT bu.user_id) FILTER (
                           WHERE bu.first_seen > NOW() - INTERVAL '7 days'
                       ) AS new_7d
                   FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                   WHERE mb.added_by = $1
                     AND mb.is_active = TRUE
                     AND bu.is_active = TRUE""",
                (owner_id,),
            ),
            # Account health
            (
                """SELECT
                       AVG(trust_score) AS avg_trust,
                       COUNT(*) FILTER (
                           WHERE trust_score < 0.3 AND is_active = TRUE
                       ) AS at_risk
                   FROM tg_accounts
                   WHERE owner_id = $1""",
                (owner_id,),
            ),
            # Revenue estimate (last 30 days)
            (
                """SELECT COALESCE(SUM(amount_usd), 0) AS total_usd
                   FROM payments
                   WHERE owner_id = $1
                     AND status = 'confirmed'
                     AND created_at > NOW() - INTERVAL '30 days'""",
                (owner_id,),
            ),
        ]

        results = await _run_concurrent(pool, queries)
        acc, ops, running, audience, health, revenue = results

        return {
            "accounts": {
                "total": _int(acc[0]["total"]),
                "active": _int(acc[0]["active"]),
                "banned": _int(acc[0]["banned"]),
                "spamblock": _int(acc[0]["spamblock"]),
            },
            "operations_24h": {
                "total": _int(ops[0]["total"]),
                "success": _int(ops[0]["success"]),
                "failed": _int(ops[0]["failed"]),
                "running": _int(running[0]["running"]),
            },
            "audience": {
                "total_users": _int(audience[0]["total_users"]),
                "new_24h": _int(audience[0]["new_24h"]),
                "new_7d": _int(audience[0]["new_7d"]),
            },
            "health": {
                "avg_trust": round(float(health[0]["avg_trust"] or 0), 2),
                "accounts_at_risk": _int(health[0]["at_risk"]),
            },
            "revenue_30d_usd": float(revenue[0]["total_usd"]),
        }


async def get_realtime_metrics(pool: asyncpg.Pool, owner_id: int) -> dict[str, Any]:
    """Метрики в реальном времени — текущее состояние и последние события.

    Возвращает:
        - active_operations: текущие выполняющиеся операции
        - recent_events: последние 10 событий (аудит/лог)
        - account_status: распределение статусов аккаунтов
        - queue_depth: глубина очереди операций
    """
    with timed(log, "get_realtime_metrics", extra={"owner_id": owner_id}):
        queries = [
            # Active operations
            (
                """SELECT
                       oq.id,
                       oq.op_type,
                       oq.status,
                       oq.started_at,
                       oq.params
                   FROM operation_queue oq
                   WHERE oq.owner_id = $1
                     AND oq.status IN ('running', 'pending')
                   ORDER BY oq.started_at DESC NULLS LAST
                   LIMIT 20""",
                (owner_id,),
            ),
            # Recent events
            (
                """SELECT
                       id,
                       action,
                       result,
                       target,
                       occurred_at
                   FROM operation_audit
                   WHERE owner_id = $1
                   ORDER BY occurred_at DESC
                   LIMIT 10""",
                (owner_id,),
            ),
            # Account status distribution
            (
                """SELECT
                       COALESCE(acc_status, 'active') AS status,
                       COUNT(*) AS cnt
                   FROM tg_accounts
                   WHERE owner_id = $1
                   GROUP BY status""",
                (owner_id,),
            ),
            # Queue depth by type
            (
                """SELECT
                       op_type,
                       COUNT(*) AS cnt
                   FROM operation_queue
                   WHERE owner_id = $1
                     AND status IN ('pending', 'running')
                   GROUP BY op_type""",
                (owner_id,),
            ),
        ]

        results = await _run_concurrent(pool, queries)
        active_ops, events, status_dist, queue_depth = results

        return {
            "active_operations": [
                {
                    "id": r["id"],
                    "type": r["op_type"],
                    "status": r["status"],
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "params": _safe_json(r.get("params")),
                }
                for r in active_ops
            ],
            "recent_events": [
                {
                    "id": r["id"],
                    "action": r["action"],
                    "result": r["result"],
                    "target": r["target"],
                    "occurred_at": r["occurred_at"].isoformat() if r["occurred_at"] else None,
                }
                for r in events
            ],
            "account_status": {
                r["status"]: r["cnt"] for r in status_dist
            },
            "queue_depth": {
                r["op_type"]: r["cnt"] for r in queue_depth
            },
        }


async def get_historical_data(
    pool: asyncpg.Pool,
    owner_id: int,
    metric: Literal["operations", "accounts", "audience", "health"],
    days: int = 30,
) -> list[dict[str, Any]]:
    """Исторические данные по выбранной метрике за N дней.

    Поддерживаемые метрики:
        - operations: количество операций по дням
        - accounts: количество аккаунтов по дням
        - audience: аудитория по дням (изменения)
        - health: средний trust_score по дням

    Возвращает список {date, value} за каждый день.
    """
    if days < 1:
        days = 1
    elif days > 365:
        days = 365

    with timed(log, "get_historical_data", extra={
        "owner_id": owner_id,
        "metric": metric,
        "days": days,
    }):
        if metric == "operations":
            return await _history_operations(pool, owner_id, days)
        elif metric == "accounts":
            return await _history_accounts(pool, owner_id, days)
        elif metric == "audience":
            return await _history_audience(pool, owner_id, days)
        elif metric == "health":
            return await _history_health(pool, owner_id, days)
        else:
            return []


async def export_analytics(
    pool: asyncpg.Pool,
    owner_id: int,
    format: Literal["json", "csv"] = "json",
) -> str:
    """Экспорт полной аналитики в JSON или CSV.

    Включает:
        - Сводку дашборда
        - Метрики в реальном времени
        - Историю операций за 30 дней
    """
    with timed(log, "export_analytics", extra={"owner_id": owner_id, "format": format}):
        dashboard = await get_dashboard_stats(pool, owner_id)
        realtime = await get_realtime_metrics(pool, owner_id)
        ops_history = await get_historical_data(pool, owner_id, "operations", 30)

        export_data = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "owner_id": owner_id,
            "dashboard": dashboard,
            "realtime": realtime,
            "operations_history_30d": ops_history,
        }

        if format == "csv":
            return _to_csv(export_data)
        return json.dumps(export_data, default=str, ensure_ascii=False, indent=2)


# ── Internal helpers ────────────────────────────────────────────────────


async def _run_concurrent(
    pool: asyncpg.Pool,
    queries: list[tuple[str, tuple]],
) -> list[list[asyncpg.Record]]:
    """Выполнить несколько независимых запросов параллельно."""
    async with pool.acquire() as conn:
        async def _safe_fetch(sql: str, params: tuple) -> list[asyncpg.Record]:
            try:
                return list(await conn.fetch(sql, *params))
            except Exception:
                log.debug("_run_concurrent: query failed: %.100s", sql, exc_info=True)
                return []
        results = await asyncio.gather(
            *(_safe_fetch(sql, params) for sql, params in queries)
        )
        return list(results)


import asyncio


def _int(val: Any) -> int:
    """Безопасное приведение к int."""
    try:
        return int(val or 0)
    except (TypeError, ValueError):
        return 0


def _safe_json(val: Any) -> Any:
    """Парсинг JSON из БД (asyncpg может вернуть str или dict)."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


async def _history_operations(
    pool: asyncpg.Pool, owner_id: int, days: int
) -> list[dict[str, Any]]:
    """Операции по дням за N дней."""
    rows = await pool.fetch(
        """SELECT
               DATE(occurred_at) AS day,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE result = 'success') AS success,
               COUNT(*) FILTER (WHERE result = 'failed') AS failed
           FROM operation_audit
           WHERE owner_id = $1
             AND occurred_at > NOW() - ($2 || ' days')::INTERVAL
           GROUP BY day
           ORDER BY day""",
        owner_id,
        str(days),
    )
    return [
        {
            "date": r["day"].isoformat() if r["day"] else None,
            "total": _int(r["total"]),
            "success": _int(r["success"]),
            "failed": _int(r["failed"]),
        }
        for r in rows
    ]


async def _history_accounts(
    pool: asyncpg.Pool, owner_id: int, days: int
) -> list[dict[str, Any]]:
    """Количество аккаунтов по дням (snapshots из health history или по added_at)."""
    try:
        rows = await pool.fetch(
            """SELECT
                   DATE(recorded_at) AS day,
                   COUNT(DISTINCT account_id) AS total
               FROM account_health_history
               WHERE owner_id = $1
                 AND recorded_at > NOW() - ($2 || ' days')::INTERVAL
               GROUP BY day
               ORDER BY day""",
            owner_id,
            str(days),
        )
        if rows:
            return [
                {"date": r["day"].isoformat(), "total": _int(r["total"])}
                for r in rows
            ]
    except Exception:
        log_exc_swallow(log, "_history_accounts: health_history not available")

    # Fallback: аккаунты по дате добавления
    rows = await pool.fetch(
        """SELECT
               DATE(added_at) AS day,
               COUNT(*) AS total
           FROM tg_accounts
           WHERE owner_id = $1
             AND added_at > NOW() - ($2 || ' days')::INTERVAL
           GROUP BY day
           ORDER BY day""",
        owner_id,
        str(days),
    )
    cumulative = 0
    result = []
    for r in rows:
        cumulative += _int(r["total"])
        result.append({
            "date": r["day"].isoformat(),
            "total": cumulative,
        })
    return result


async def _history_audience(
    pool: asyncpg.Pool, owner_id: int, days: int
) -> list[dict[str, Any]]:
    """Аудитория по дням (новые пользователи)."""
    rows = await pool.fetch(
        """SELECT
               DATE(bu.first_seen) AS day,
               COUNT(DISTINCT bu.user_id) AS new_users
           FROM bot_users bu
           JOIN managed_bots mb ON mb.bot_id = bu.bot_id
           WHERE mb.added_by = $1
             AND mb.is_active = TRUE
             AND bu.first_seen > NOW() - ($2 || ' days')::INTERVAL
           GROUP BY day
           ORDER BY day""",
        owner_id,
        str(days),
    )
    return [
        {"date": r["day"].isoformat(), "new_users": _int(r["new_users"])}
        for r in rows
    ]


async def _history_health(
    pool: asyncpg.Pool, owner_id: int, days: int
) -> list[dict[str, Any]]:
    """Средний trust_score по дням."""
    rows = await pool.fetch(
        """SELECT
               DATE(recorded_at) AS day,
               AVG(health_score) AS avg_health,
               AVG(trust_score) AS avg_trust
           FROM account_health_history
           WHERE owner_id = $1
             AND recorded_at > NOW() - ($2 || ' days')::INTERVAL
           GROUP BY day
           ORDER BY day""",
        owner_id,
        str(days),
    )
    return [
        {
            "date": r["day"].isoformat(),
            "avg_health": round(float(r["avg_health"] or 0), 2),
            "avg_trust": round(float(r["avg_trust"] or 0), 2),
        }
        for r in rows
    ]


def _to_csv(data: dict[str, Any]) -> str:
    """Конвертация данных экспорта в CSV."""
    output = io.StringIO()

    # Dashboard summary
    output.write("=== Dashboard Summary ===\n")
    writer = csv.writer(output)
    writer.writerow(["Metric", "Value"])
    dash = data.get("dashboard", {})
    for section, values in dash.items():
        if isinstance(values, dict):
            for k, v in values.items():
                writer.writerow([f"{section}.{k}", v])
        else:
            writer.writerow([section, values])

    # Operations history
    output.write("\n=== Operations History (30d) ===\n")
    history = data.get("operations_history_30d", [])
    if history:
        writer = csv.DictWriter(output, fieldnames=history[0].keys())
        writer.writeheader()
        writer.writerows(history)

    return output.getvalue()
