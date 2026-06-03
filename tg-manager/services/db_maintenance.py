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
    ("behavioral_events",  "occurred_at",  "90 days"),
    ("operation_log",      "created_at",   "30 days"),
    ("restriction_events", "created_at",   "90 days"),
    ("account_flood_log",  "created_at",   "30 days"),
    ("search_rankings",    "checked_at",   "90 days"),
    ("search_snapshots",   "checked_at",   "14 days"),
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
                log.info("db_maintenance: pruned %d rows from %s (>%s)", n, table, interval)
        except Exception as e:
            log.warning("db_maintenance: failed to prune %s: %s", table, e)
            results[table] = -1

    # Completed operation_queue entries — but only if operation_log entries are
    # also gone (FK safety: operation_log.op_id refs operation_queue.id).
    # We prune operation_log first (above), then queue entries.
    try:
        deleted = await pool.fetchval(
            "WITH d AS (DELETE FROM operation_queue "
            "WHERE status = ANY($1::text[]) "
            "  AND created_at < NOW() - INTERVAL $2 "
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
                n, _OPERATION_QUEUE_RETENTION,
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
