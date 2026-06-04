"""Activity Logger — non-blocking async queue writer for activity_log table.

log_event() is sync and never blocks. Background run() batch-inserts every 2s.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

_queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
_BATCH_INTERVAL = 2.0
_BATCH_SIZE = 500


def log_event(
    owner_id: int,
    event_type: str,
    action: str,
    detail: str | None = None,
    status: str = "ok",
    error_msg: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Fire-and-forget: enqueue activity event. Never raises, never blocks."""
    if not owner_id:
        return
    try:
        _queue.put_nowait(
            (owner_id, event_type, action, detail, status, error_msg, duration_ms)
        )
    except asyncio.QueueFull:
        pass


async def run(pool: asyncpg.Pool) -> None:
    """Background batch-insert loop. Register in main.py."""
    log.info("activity_logger: started (batch_interval=%.1fs)", _BATCH_INTERVAL)
    while True:
        await asyncio.sleep(_BATCH_INTERVAL)
        if _queue.empty():
            continue
        batch = []
        while not _queue.empty() and len(batch) < _BATCH_SIZE:
            try:
                batch.append(_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            continue
        try:
            await pool.executemany(
                """INSERT INTO activity_log
                   (owner_id, event_type, action, detail, status, error_msg, duration_ms)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                batch,
            )
        except Exception as e:
            log.debug("activity_logger: batch insert failed (%d rows): %s", len(batch), e)
