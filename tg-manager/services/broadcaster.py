"""Background broadcast runner with rate-limiting and progress tracking."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api
from config import BROADCAST_DELAY

logger = logging.getLogger(__name__)

# broadcast_id → asyncio.Task, for optional cancellation
_running: dict[int, asyncio.Task] = {}


async def run(pool: asyncpg.Pool, session: aiohttp.ClientSession,
              broadcast_id: int, token: str, bot_id: int, text: str,
              photo_file_id: str | None = None) -> None:
    user_ids = await db.get_audience_user_ids(pool, bot_id)
    sent = failed = 0
    await db.update_broadcast(pool, broadcast_id, 0, 0, "running")

    for uid in user_ids:
        if photo_file_id:
            success, retry_after = await bot_api.send_photo(session, token, uid, photo_file_id, text)
        else:
            success, retry_after = await bot_api.send_message(session, token, uid, text)
        if success:
            sent += 1
        else:
            failed += 1
            if retry_after:
                logger.info("Broadcast %d: rate-limited, sleeping %ds", broadcast_id, retry_after)
                await asyncio.sleep(retry_after)
                # Retry once after the cooldown
                if photo_file_id:
                    ok, _ = await bot_api.send_photo(session, token, uid, photo_file_id, text)
                else:
                    ok, _ = await bot_api.send_message(session, token, uid, text)
                if ok:
                    sent += 1
                    failed -= 1
                else:
                    await db.mark_user_inactive(pool, bot_id, uid)
            else:
                # 403 / user blocked the bot — deactivate
                await db.mark_user_inactive(pool, bot_id, uid)

        await asyncio.sleep(BROADCAST_DELAY)

    await db.update_broadcast(pool, broadcast_id, sent, failed, "done")
    _running.pop(broadcast_id, None)
    logger.info("Broadcast %d done: sent=%d failed=%d", broadcast_id, sent, failed)


def start(pool: asyncpg.Pool, session: aiohttp.ClientSession,
          broadcast_id: int, token: str, bot_id: int, text: str,
          photo_file_id: str | None = None) -> None:
    task = asyncio.create_task(
        run(pool, session, broadcast_id, token, bot_id, text, photo_file_id),
        name=f"broadcast-{broadcast_id}",
    )
    _running[broadcast_id] = task


def cancel(broadcast_id: int) -> bool:
    task = _running.get(broadcast_id)
    if task and not task.done():
        task.cancel()
        _running.pop(broadcast_id, None)
        return True
    return False


def is_running(broadcast_id: int) -> bool:
    task = _running.get(broadcast_id)
    return task is not None and not task.done()
