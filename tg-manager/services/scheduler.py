"""Background scheduler: fires due scheduled broadcasts every 60 seconds."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import broadcaster

log = logging.getLogger(__name__)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            rows = await db.get_pending_scheduled(pool)
            for row in rows:
                await db.mark_scheduled_done(pool, row["id"])
                total = await db.get_audience_count(pool, row["bot_id"])
                bc_id = await db.create_broadcast(
                    pool, row["bot_id"], row["message_text"], total, row["created_by"]
                )
                broadcaster.start(
                    pool, http, bc_id, row["token"], row["bot_id"], row["message_text"]
                )
                log.info(
                    "Scheduled #%d fired → broadcast #%d (bot %d)",
                    row["id"], bc_id, row["bot_id"],
                )
        except Exception:
            log.exception("Scheduler loop error")
        await asyncio.sleep(60)
