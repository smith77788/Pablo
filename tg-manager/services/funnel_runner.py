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
                    "UPDATE funnel_subscribers SET conversion_recorded=true WHERE id=$1",
                    sub_id,
                )
        # Пометить воронку завершённой
        await pool.execute(
            "UPDATE funnel_subscribers SET completed_at=now() WHERE id=$1 AND completed_at IS NULL",
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
                    ok, _ = await bot_api.send_message(
                        http, row["token"], row["user_id"], row["message_text"]
                    )
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
