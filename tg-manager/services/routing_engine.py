"""Swarm routing engine: decides whether to redirect users between bots based on mode and scores."""

from __future__ import annotations
import logging
import random
import aiohttp
import asyncpg
from database import db
from services import bot_api

log = logging.getLogger(__name__)


async def make_routing_decision(
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    bot_id: int,
    user_id: int,
    chat_id: int,
    token: str,
    cluster: str,
) -> bool:
    """
    Called when a new /start is received by an 'entry' bot in swarm.
    Returns True if user was routed to another bot.
    """
    try:
        mode = await db.get_system_mode(pool)
        config = await db.get_mode_routing_config(mode)

        if not config["routing_enabled"]:
            await db.log_routing_decision(pool, bot_id, None, user_id, "kept", mode)
            return False

        # Probabilistic routing
        if random.random() > config["routing_probability"]:
            await db.log_routing_decision(pool, bot_id, None, user_id, "kept", mode)
            return False

        # Find target bot using weighted random selection
        target = await db.get_weighted_routing_target(pool, cluster, bot_id)
        if not target:
            await db.log_routing_decision(
                pool, bot_id, None, user_id, "no_target", mode
            )
            return False

        score_target = target.get("score", 0) or 0

        # Only route if target meets minimum score threshold
        if (
            score_target < config["min_score_threshold"]
            and config["min_score_threshold"] > 0
        ):
            await db.log_routing_decision(pool, bot_id, None, user_id, "kept", mode)
            return False

        # Get own score
        own_metrics = await pool.fetchrow(
            "SELECT score FROM bot_metrics WHERE bot_id=$1", bot_id
        )
        score_own = own_metrics["score"] if own_metrics else 0

        # Route user: send referral link to the target bot
        target_label = (
            f"@{target['username']}"
            if target.get("username")
            else str(target["bot_id"])
        )
        route_msg = (
            f"🔀 Для наилучшего опыта, перейдите в нашего специализированного бота:\n\n"
            f"👉 {target_label}\n\n"
            f"<i>Нажмите на username, чтобы открыть бота.</i>"
        )
        if target.get("username"):
            route_msg += f"\n\nhttps://t.me/{target['username']}"

        ok, _ = await bot_api.send_message(http, token, chat_id, route_msg)
        if ok:
            await db.log_routing_decision(
                pool,
                bot_id,
                target["bot_id"],
                user_id,
                "routed",
                mode,
                score_own,
                score_target,
            )
            log.info(
                "Routed user %d from bot %d to bot %d (mode=%s)",
                user_id,
                bot_id,
                target["bot_id"],
                mode,
            )
            return True
        else:
            await db.log_routing_decision(pool, bot_id, None, user_id, "kept", mode)
            return False

    except Exception:
        log.exception("Routing decision error for bot %d user %d", bot_id, user_id)
        return False
