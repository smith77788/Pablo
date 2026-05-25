"""Background funnel (message chain) runner service."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api

log = logging.getLogger(__name__)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            due = await db.get_due_funnel_steps(pool)
            for row in due:
                try:
                    ok, _ = await bot_api.send_message(
                        http, row["token"], row["user_id"], row["message_text"]
                    )
                    next_step = row["current_step"] + 1
                    # Get delay for the next step
                    steps = await db.get_funnel_steps(pool, row["funnel_id"])
                    next_delay = steps[next_step]["delay_minutes"] if next_step < len(steps) else 0
                    await db.advance_funnel_step(
                        pool, row["sub_id"], next_step, row["total_steps"], next_delay
                    )
                except Exception:
                    log.exception("Funnel runner error for sub_id=%s", row["sub_id"])
        except Exception:
            log.exception("Funnel runner loop error")
        await asyncio.sleep(60)
