"""Database Maintenance — data retention and index health.

Runs every 6 hours. Prunes append-only tables that have no TTL:
  - behavioral_events       → keep 90 days
  - operation_log           → keep 30 days  (per-step logs of operations)
  - restriction_events      → keep 90 days
  - account_flood_log       → keep 30 days
  - search_rankings         → keep 90 days  (trend data for charts)
  - search_snapshots        → keep 14 days  (raw JSON, very heavy)
  - operation_queue done    → keep 30 days  (completed/failed/cancelled/skipped)

Without this, a platform with 50 active users generating ~200 events/day fills
behavioral_events with 900 000+ rows in 90 days and search_snapshots can hit
gigabytes within months.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

_RETENTION: list[tuple[str, str, str]] = [
    # (table, timestamp_column, interval)
    ("behavioral_events", "occurred_at", "90 days"),
    ("operation_log", "created_at", "30 days"),
    ("restriction_events", "created_at", "90 days"),
    ("account_flood_log", "created_at", "30 days"),
    ("search_rankings", "checked_at", "90 days"),
    ("search_snapshots", "captured_at", "14 days"),
    # EPOCH VI tables
    ("recovery_events", "created_at", "30 days"),
    ("anomaly_events", "detected_at", "14 days"),
    ("system_health_snapshots", "snapshot_at", "7 days"),
    ("infrastructure_alerts", "first_seen_at", "30 days"),
    # Proxy telemetry
    ("proxy_quality_log", "checked_at", "30 days"),
    ("proxy_health_log", "checked_at", "30 days"),
    # Operation audit
    ("operation_audit", "occurred_at", "60 days"),
    # Activity log (UI events) — keep 14 days
    ("activity_log", "occurred_at", "14 days"),
]

_OPERATION_QUEUE_RETENTION = "30 days"
_DONE_STATUSES = ("done", "failed", "cancelled", "skipped", "missed")


async def run_once(pool: asyncpg.Pool) -> dict[str, int]:
    """Execute one maintenance pass. Returns {table: rows_deleted}."""
    results: dict[str, int] = {}

    for table, ts_col, interval in _RETENTION:
        try:
            deleted = await pool.fetchval(
                f"WITH d AS (DELETE FROM {table} "
                f"WHERE {ts_col} < NOW() - INTERVAL '{interval}' RETURNING 1) "
                f"SELECT COUNT(*) FROM d"
            )
            n = int(deleted or 0)
            results[table] = n
            if n:
                log.info(
                    "db_maintenance: pruned %d rows from %s (>%s)", n, table, interval
                )
        except Exception as e:
            log.warning("db_maintenance: failed to prune %s: %s", table, e)
            results[table] = -1

    # Orphaned infra_memory rows — rows whose account/proxy no longer exists.
    # infra_memory_accounts has no FK to tg_accounts, so deleted accounts leave
    # dangling rows. Clean them up; also prune rows inactive for >90 days.
    try:
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM infra_memory_accounts "
            "WHERE NOT EXISTS (SELECT 1 FROM tg_accounts WHERE id = infra_memory_accounts.account_id) "
            "   OR updated_at < NOW() - INTERVAL '90 days' "
            "RETURNING 1) "
            "SELECT COUNT(*) FROM d"
        )
        n = int(deleted or 0)
        results["infra_memory_accounts(orphan)"] = n
        if n:
            log.info(
                "db_maintenance: pruned %d orphaned/stale rows from infra_memory_accounts", n
            )
    except Exception as e:
        log.warning("db_maintenance: failed to prune infra_memory_accounts: %s", e)
        results["infra_memory_accounts(orphan)"] = -1

    try:
        # user_proxies.proxy_url зашифрован → SQL-equality join с infra_memory_proxies
        # (plaintext-ключи) невозможен. Считаем «сирот» в Python: decrypt активных
        # user-прокси, затем удаляем строки памяти, которых нет среди них, ИЛИ старые.
        from services.token_vault import decrypt_token

        _user_rows = await pool.fetch("SELECT proxy_url FROM user_proxies")
        _active = {
            decrypt_token(r["proxy_url"]) for r in _user_rows if r["proxy_url"]
        }
        _im_urls = await pool.fetch("SELECT DISTINCT proxy_url FROM infra_memory_proxies")
        _orphans = [r["proxy_url"] for r in _im_urls if r["proxy_url"] not in _active]
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM infra_memory_proxies "
            "WHERE proxy_url = ANY($1::text[]) "
            "   OR updated_at < NOW() - INTERVAL '90 days' "
            "RETURNING 1) "
            "SELECT COUNT(*) FROM d",
            _orphans,
        )
        n = int(deleted or 0)
        results["infra_memory_proxies(orphan)"] = n
        if n:
            log.info(
                "db_maintenance: pruned %d orphaned/stale rows from infra_memory_proxies", n
            )
    except Exception as e:
        log.warning("db_maintenance: failed to prune infra_memory_proxies: %s", e)
        results["infra_memory_proxies(orphan)"] = -1

    # Orphaned account-scoped state/stats — эти таблицы ссылаются на tg_accounts.id
    # БЕЗ внешнего ключа, поэтому удаление аккаунта (mini_app account_delete / bulk
    # delete) оставляет висячие строки. Consumers их не обрабатывают (join на
    # tg_accounts отфильтровывает), но без чистки они копятся бесконечно. У
    # operation_audit своё истечение (>30 дн), infra_memory чистится выше —
    # поэтому здесь только эти три. managed_channels НЕ трогаем: канал — отдельная
    # владеемая сущность, его судьба при удалении аккаунта — продуктовое решение.
    for _tbl, _col in (
        ("account_status_events", "acc_id"),
        ("account_daily_stats", "account_id"),
        ("account_rehab_state", "acc_id"),
    ):
        try:
            deleted = await pool.fetchval(
                f"WITH d AS (DELETE FROM {_tbl} t "
                f"WHERE NOT EXISTS (SELECT 1 FROM tg_accounts a WHERE a.id = t.{_col}) "
                f"RETURNING 1) SELECT COUNT(*) FROM d"
            )
            n = int(deleted or 0)
            results[f"{_tbl}(orphan)"] = n
            if n:
                log.info("db_maintenance: pruned %d orphaned rows from %s", n, _tbl)
        except Exception as e:
            log.warning("db_maintenance: failed to prune %s: %s", _tbl, e)
            results[f"{_tbl}(orphan)"] = -1

    # Completed operation_queue entries — but only if operation_log entries are
    # also gone (FK safety: operation_log.op_id refs operation_queue.id).
    # We prune operation_log first (above), then queue entries.
    try:
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM operation_queue "
            "WHERE status = ANY($1::text[]) "
            "  AND created_at < NOW() - $2::INTERVAL "
            "  AND NOT EXISTS ("
            "      SELECT 1 FROM operation_log WHERE op_id = operation_queue.id"
            "  ) "
            "RETURNING 1) "
            "SELECT COUNT(*) FROM d",
            list(_DONE_STATUSES),
            _OPERATION_QUEUE_RETENTION,
        )
        n = int(deleted or 0)
        results["operation_queue(done)"] = n
        if n:
            log.info(
                "db_maintenance: pruned %d completed operations from operation_queue (>%s)",
                n,
                _OPERATION_QUEUE_RETENTION,
            )
    except Exception as e:
        log.warning("db_maintenance: failed to prune operation_queue: %s", e)
        results["operation_queue(done)"] = -1

    return results


async def run(pool: asyncpg.Pool, *, interval_hours: float = 6.0) -> None:
    """Background loop: run maintenance every interval_hours."""
    log.info("db_maintenance: started (interval=%gh)", interval_hours)
    # Initial delay — let the bot warm up before touching the DB
    await asyncio.sleep(300)

    while True:
        try:
            results = await run_once(pool)
            total = sum(v for v in results.values() if v > 0)
            if total:
                log.info("db_maintenance: total %d rows pruned this pass", total)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("db_maintenance: unexpected error: %s", e, exc_info=True)

        await asyncio.sleep(interval_hours * 3600)
