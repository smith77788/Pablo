"""Auto-Funnel — automated message sequences delivered to bot audience segments."""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import asyncpg
from aiogram import Bot

from services import bot_api

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 60   # seconds between processing cycles
_BATCH_SIZE    = 50   # max runs to process per cycle


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    ssl_import = None
    try:
        import ssl as ssl_module
        import aiohttp as _aiohttp
        ssl_ctx = ssl_module.SSLContext(ssl_module.PROTOCOL_TLS_CLIENT)
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl_module.CERT_NONE
        connector = _aiohttp.TCPConnector(ssl=ssl_ctx)
        http = _aiohttp.ClientSession(connector=connector)
    except Exception:
        http = aiohttp.ClientSession()

    log.info("Auto-Funnel service started")
    try:
        while True:
            try:
                await _process_due_runs(pool, http)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Auto-Funnel loop error: %s", e)
            await asyncio.sleep(_LOOP_INTERVAL)
    finally:
        await http.close()


async def _process_due_runs(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    runs = await pool.fetch(
        """
        SELECT r.*, f.bot_id AS f_bot_id
        FROM auto_funnel_runs r
        JOIN auto_funnels f ON f.id = r.funnel_id
        WHERE r.status = 'active'
          AND r.next_send_at <= NOW()
        ORDER BY r.next_send_at
        LIMIT $1
        """,
        _BATCH_SIZE,
    )
    for run in runs:
        try:
            await _process_run(pool, http, run)
        except Exception as e:
            log.debug("Auto-Funnel: run %d error: %s", run["id"], e)
            await pool.execute(
                "UPDATE auto_funnel_runs SET status='error' WHERE id=$1",
                run["id"],
            )


async def _process_run(pool: asyncpg.Pool, http: aiohttp.ClientSession, run) -> None:
    funnel_id = run["funnel_id"]
    step_num  = run["next_step_num"]

    step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 AND step_num=$2",
        funnel_id, step_num,
    )
    if not step:
        # No more steps — mark completed
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='completed' WHERE id=$1", run["id"]
        )
        return

    # Get bot token
    from database.db import fetchrow_bot as _fetchrow_bot_af
    bot_row = await _fetchrow_bot_af(
        pool, "SELECT token FROM managed_bots WHERE bot_id=$1", run["bot_id"]
    )
    if not bot_row:
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='error' WHERE id=$1", run["id"]
        )
        return

    # Build optional button
    buttons = None
    if step["button_text"] and step["button_url"]:
        buttons = [{"text": step["button_text"], "url": step["button_url"]}]

    # Send message
    ok = False
    try:
        ok = await bot_api.send_message(
            http,
            bot_row["token"],
            run["user_id"],
            step["message_text"],
            buttons=buttons,
        )
    except Exception as e:
        log.debug("Auto-Funnel: send error run %d: %s", run["id"], e)

    if not ok:
        # User likely blocked the bot — stop this run
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='stopped' WHERE id=$1", run["id"]
        )
        return

    # Find next step
    next_step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 AND step_num > $2 ORDER BY step_num LIMIT 1",
        funnel_id, step_num,
    )
    if not next_step:
        await pool.execute(
            "UPDATE auto_funnel_runs SET status='completed' WHERE id=$1", run["id"]
        )
        return

    # Schedule next step
    from datetime import timedelta
    next_at = datetime.now(timezone.utc) + timedelta(hours=next_step["delay_hours"])
    await pool.execute(
        "UPDATE auto_funnel_runs SET next_step_num=$1, next_send_at=$2 WHERE id=$3",
        next_step["step_num"], next_at, run["id"],
    )


async def launch_funnel(
    pool: asyncpg.Pool,
    funnel_id: int,
    owner_id: int,
    segment: str,
) -> int:
    """Enqueue runs for all matching users in the segment. Returns count of new runs."""
    funnel = await pool.fetchrow(
        "SELECT * FROM auto_funnels WHERE id=$1 AND owner_id=$2",
        funnel_id, owner_id,
    )
    if not funnel:
        return 0

    # Get first step to know initial delay
    first_step = await pool.fetchrow(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num LIMIT 1",
        funnel_id,
    )
    if not first_step:
        return 0

    from datetime import timedelta

    first_send_at = datetime.now(timezone.utc) + timedelta(hours=first_step["delay_hours"])

    # Segment query
    if segment == "all":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE",
            funnel["bot_id"],
        )
    elif segment == "new_7d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND first_seen >= NOW() - INTERVAL '7 days'",
            funnel["bot_id"],
        )
    elif segment == "new_30d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND first_seen >= NOW() - INTERVAL '30 days'",
            funnel["bot_id"],
        )
    elif segment == "inactive_30d":
        users = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE AND last_seen < NOW() - INTERVAL '30 days'",
            funnel["bot_id"],
        )
    else:
        return 0

    if not users:
        return 0

    # Bulk upsert runs (ignore existing to avoid re-sending)
    count = 0
    for u in users:
        result = await pool.execute(
            """
            INSERT INTO auto_funnel_runs (funnel_id, bot_id, user_id, next_step_num, next_send_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (funnel_id, user_id) DO NOTHING
            """,
            funnel_id, funnel["bot_id"], u["user_id"],
            first_step["step_num"], first_send_at,
        )
        if "INSERT 0 1" in result:
            count += 1

    return count
