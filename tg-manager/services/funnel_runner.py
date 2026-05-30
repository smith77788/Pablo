"""Background funnel (message chain) runner service."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api

log = logging.getLogger(__name__)


async def _record_funnel_conversion(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    funnel_id: int,
    sub_id: int,
) -> None:
    """Записать реферальную конверсию когда пользователь завершает воронку."""
    try:
        # Найти реферрера для этого пользователя в данном боте
        ref_row = await pool.fetchrow(
            "SELECT referrer_user_id FROM referrals WHERE bot_id=$1 AND referred_user_id=$2 LIMIT 1",
            bot_id, user_id,
        )
        if ref_row and ref_row["referrer_user_id"] != user_id:
            # Проверить дубли
            exists = await pool.fetchval(
                "SELECT 1 FROM referral_conversions "
                "WHERE bot_id=$1 AND referred_id=$2 AND funnel_id=$3 AND conversion_type='funnel_complete'",
                bot_id, user_id, funnel_id,
            )
            if not exists:
                await pool.execute(
                    """INSERT INTO referral_conversions(bot_id, referrer_id, referred_id, conversion_type, funnel_id)
                       VALUES($1,$2,$3,'funnel_complete',$4)""",
                    bot_id, ref_row["referrer_user_id"], user_id, funnel_id,
                )
                await pool.execute(
                    "UPDATE funnel_subscriptions SET conversion_recorded=true WHERE id=$1",
                    sub_id,
                )
        # Пометить воронку завершённой
        await pool.execute(
            "UPDATE funnel_subscriptions SET completed_at=now() WHERE id=$1 AND completed_at IS NULL",
            sub_id,
        )
    except Exception as e:
        log.debug("funnel conversion record error: %s", e)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            due = await db.get_due_funnel_steps(pool)
            for row in due:
                try:
                    # Try up to 3 times with backoff before giving up on this step
                    sent_ok = False
                    retry_info = ""
                    for attempt in range(3):
                        ok, retry_after = await bot_api.send_message(
                            http, row["token"], row["user_id"], row["message_text"]
                        )
                        if ok:
                            sent_ok = True
                            break
                        if retry_after:
                            log.info("Funnel step %d for sub %d rate-limited, sleeping %ds",
                                     row["current_step"] + 1, row["sub_id"], retry_after)
                            await asyncio.sleep(retry_after)
                            retry_info = f" (retry {attempt + 1}, {retry_after}s)"
                            continue
                        # Non-429 failure (user blocked, bot kicked, etc.) — don't retry
                        log.warning("Funnel step %d for sub %d failed: non-retryable",
                                    row["current_step"] + 1, row["sub_id"])
                        break

                    if not sent_ok:
                        # Don't advance — will retry on next loop iteration
                        log.warning("Funnel step %d for sub %d skipped after retries, will retry",
                                    row["current_step"] + 1, row["sub_id"])
                        continue

                    next_step = row["current_step"] + 1
                    steps = await db.get_funnel_steps(pool, row["funnel_id"])
                    is_last = next_step >= row["total_steps"]
                    next_delay = steps[next_step]["delay_minutes"] if next_step < len(steps) else 0
                    await db.advance_funnel_step(
                        pool, row["sub_id"], next_step, row["total_steps"], next_delay
                    )
                    # Фиксировать конверсию при завершении воронки
                    if is_last:
                        asyncio.ensure_future(
                            _record_funnel_conversion(
                                pool, row["bot_id"], row["user_id"],
                                row["funnel_id"], row["sub_id"],
                            )
                        )
                except Exception:
                    log.exception("Funnel runner error for sub_id=%s", row["sub_id"])
        except Exception:
            log.exception("Funnel runner loop error")
        await asyncio.sleep(60)
