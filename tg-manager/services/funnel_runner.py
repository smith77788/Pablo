"""Background funnel (message chain) runner service."""

from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api, brand_injection

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
            bot_id,
            user_id,
        )
        if ref_row and ref_row["referrer_user_id"] != user_id:
            # Проверить дубли
            exists = await pool.fetchval(
                "SELECT 1 FROM referral_conversions "
                "WHERE bot_id=$1 AND referred_id=$2 AND funnel_id=$3 AND conversion_type='funnel_complete'",
                bot_id,
                user_id,
                funnel_id,
            )
            if not exists:
                await pool.execute(
                    """INSERT INTO referral_conversions(bot_id, referrer_id, referred_id, conversion_type, funnel_id)
                       VALUES($1,$2,$3,'funnel_complete',$4)""",
                    bot_id,
                    ref_row["referrer_user_id"],
                    user_id,
                    funnel_id,
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
        log.warning("funnel conversion record error: %s", e)


async def run_once(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Process all due funnel steps exactly once (one iteration, no loop).

    Called from the background ``run`` loop and also usable in tests /
    manual invocations.
    """
    try:
        due = await db.get_due_funnel_steps(pool)
        _steps_cache: dict[int, list] = {}
        for row in due:
            try:
                # Try up to 3 times with backoff before giving up on this step
                sent_ok = False
                rate_limited = False
                permanently_failed = False
                # Brand injection for free-tier bots
                _msg_text = row["message_text"]
                try:
                    if await brand_injection.is_free_tier(pool, row["bot_id"]):
                        _msg_text = brand_injection.add_promo(_msg_text, html=True, context="broadcast")
                except Exception:
                    pass
                for attempt in range(3):
                    ok, retry_after = await bot_api.send_message(
                        http, row["token"], row["user_id"], _msg_text
                    )
                    if ok:
                        sent_ok = True
                        break
                    if retry_after:
                        rate_limited = True
                        log.info(
                            "Funnel step %d for sub %d rate-limited, sleeping %ds",
                            row["current_step"] + 1,
                            row["sub_id"],
                            retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    # Non-429 failure (403 blocked, user deactivated, etc.) — don't retry
                    permanently_failed = True
                    log.warning(
                        "Funnel step %d for sub %d failed: non-retryable (bot blocked or user deactivated)",
                        row["current_step"] + 1,
                        row["sub_id"],
                    )
                    break

                if not sent_ok:
                    if permanently_failed:
                        # 403 / non-retryable: mark dropped so the subscription
                        # is excluded from future get_due_funnel_steps queries.
                        log.warning(
                            "Funnel step %d for sub %d failed permanently, marking dropped",
                            row["current_step"] + 1,
                            row["sub_id"],
                        )
                        try:
                            await pool.execute(
                                "UPDATE funnel_subscriptions SET dropped=true, completed_at=now() "
                                "WHERE id=$1 AND completed=false",
                                row["sub_id"],
                            )
                            await pool.execute(
                                "UPDATE funnels SET dropped_count = dropped_count + 1 WHERE id=$1",
                                row["funnel_id"],
                            )
                        except Exception as _drop_err:
                            log.debug("funnel drop mark failed: %s", _drop_err)
                    else:
                        # Rate-limited after all retries — will retry next loop cycle
                        log.info(
                            "Funnel step %d for sub %d skipped (rate-limited), will retry",
                            row["current_step"] + 1,
                            row["sub_id"],
                        )
                    continue

                next_step = row["current_step"] + 1
                fid = row["funnel_id"]
                if fid not in _steps_cache:
                    _steps_cache[fid] = await db.get_funnel_steps(pool, fid)
                steps = _steps_cache[fid]
                is_last = next_step >= row["total_steps"]
                next_delay = (
                    steps[next_step]["delay_minutes"]
                    if next_step < len(steps)
                    else 0
                )
                await db.advance_funnel_step(
                    pool, row["sub_id"], next_step, row["total_steps"], next_delay,
                    funnel_id=row["funnel_id"],
                )
                # Record conversion when funnel completes
                if is_last:
                    asyncio.create_task(
                        _record_funnel_conversion(
                            pool,
                            row["bot_id"],
                            row["user_id"],
                            row["funnel_id"],
                            row["sub_id"],
                        )
                    )
            except Exception:
                log.exception("Funnel runner error for sub_id=%s", row["sub_id"])
    except Exception:
        log.exception("Funnel runner loop error")


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Continuous background loop: process due funnel steps every 60 seconds."""
    while True:
        await run_once(pool, http)
        await asyncio.sleep(60)
