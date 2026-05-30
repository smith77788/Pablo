"""Background broadcast runner with rate-limiting and progress tracking."""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime

import aiohttp
import asyncpg
from database import db
from services import bot_api
from config import BROADCAST_DELAY

from bot.utils.template_validator import replace_placeholders, list_placeholders

logger = logging.getLogger(__name__)

# broadcast_id → asyncio.Task, for optional cancellation
_running: dict[int, asyncio.Task] = {}


def _render_for_user(text: str, user_info: dict, bot_name: str = "") -> str:
    """Render {{PLACEHOLDER}} tokens for a specific user."""
    if not text or "{{" not in text:
        return text
    username = user_info.get("username", "") or ""
    first_name = user_info.get("first_name", "") or ""
    last_name = user_info.get("last_name", "") or ""
    now = datetime.now()
    return replace_placeholders(text, {
        "USERNAME": f"@{username}" if username else first_name,
        "FIRST_NAME": first_name,
        "LAST_NAME": last_name,
        "FULL_NAME": f"{first_name} {last_name}".strip(),
        "BOT_NAME": bot_name,
        "DATE": now.strftime("%d.%m.%Y"),
        "DATE_SHORT": now.strftime("%d.%m"),
        "TIME": now.strftime("%H:%M"),
    })


async def run(pool: asyncpg.Pool, session: aiohttp.ClientSession,
              broadcast_id: int, token: str, bot_id: int, text: str,
              photo_file_id: str | None = None,
              user_ids: list[int] | None = None,
              buttons: list[dict] | None = None) -> None:
    if user_ids is None:
        user_ids = await db.get_audience_user_ids(pool, bot_id)
    sent = failed = 0
    await db.update_broadcast(pool, broadcast_id, 0, 0, "running")

    # Pre-load user data for placeholder rendering if needed
    has_placeholders = "{{" in text
    user_map: dict[int, dict] = {}
    if has_placeholders and user_ids:
        rows = await pool.fetch(
            "SELECT user_id, username, first_name, last_name FROM bot_users "
            "WHERE bot_id=$1 AND user_id = ANY($2::bigint[])",
            bot_id, user_ids,
        )
        user_map = {r["user_id"]: dict(r) for r in rows}
    bot_name = ""
    if has_placeholders:
        bot_row = await pool.fetchrow(
            "SELECT username, first_name FROM managed_bots WHERE bot_id=$1", bot_id
        )
        if bot_row:
            bot_name = bot_row.get("username") or bot_row.get("first_name") or ""
    user_count = len(user_ids)

    for uid in user_ids:
        # Render per-user placeholders
        user_text = text
        if has_placeholders:
            ui = user_map.get(uid, {})
            user_text = _render_for_user(text, ui, bot_name)

        if photo_file_id:
            success, retry_after = await bot_api.send_photo(
                session, token, uid, photo_file_id, user_text, buttons=buttons)
        else:
            success, retry_after = await bot_api.send_message(
                session, token, uid, user_text, buttons=buttons)
        if success:
            sent += 1
        else:
            failed += 1
            if retry_after:
                logger.info("Broadcast %d: rate-limited, sleeping %ds", broadcast_id, retry_after)
                await asyncio.sleep(retry_after)
                if photo_file_id:
                    ok, _ = await bot_api.send_photo(session, token, uid, photo_file_id, user_text, buttons=buttons)
                else:
                    ok, _ = await bot_api.send_message(session, token, uid, user_text, buttons=buttons)
                if ok:
                    sent += 1
                    failed -= 1
                else:
                    await db.mark_user_inactive(pool, bot_id, uid)
            else:
                await db.mark_user_inactive(pool, bot_id, uid)

        await asyncio.sleep(BROADCAST_DELAY)

    await db.update_broadcast(pool, broadcast_id, sent, failed, "done")
    _running.pop(broadcast_id, None)
    logger.info("Broadcast %d done: sent=%d failed=%d", broadcast_id, sent, failed)


def start(pool: asyncpg.Pool, session: aiohttp.ClientSession,
          broadcast_id: int, token: str, bot_id: int, text: str,
          photo_file_id: str | None = None,
          user_ids: list[int] | None = None,
          buttons: list[dict] | None = None) -> None:
    task = asyncio.create_task(
        run(pool, session, broadcast_id, token, bot_id, text, photo_file_id, user_ids, buttons),
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
