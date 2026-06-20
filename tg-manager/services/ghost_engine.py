"""Ghost Engine — autonomous continuous background presence for TG accounts.

Each enabled ghost_profile selects one random action per cycle based on
personality type.  Actions use only the account's existing subscriptions —
no new channel joins, no group posts.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot
from telethon.errors import (
    AuthKeyError,
    FloodWaitError,
    UserDeactivatedBanError,
    ChatWriteForbiddenError,
    PeerFloodError,
)
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import ReactionEmoji

from services.account_manager import _make_client

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 200        # seconds between full sweeps
_STAGGER_MIN   = 4          # seconds between individual account actions
_STAGGER_MAX   = 18

PERSONALITY_CAPS: dict[str, int] = {
    "ghost":   8,
    "watcher": 15,
    "active":  25,
}

_SAFE_REACTIONS = ["👍", "❤️", "🔥", "👏", "🎉", "💯", "😮"]


# ─── DB helpers ───────────────────────────────────────────────────────────────


async def _count_today_actions(pool: asyncpg.Pool, profile_id: int) -> int:
    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS cnt
        FROM ghost_action_log
        WHERE ghost_profile_id = $1
          AND executed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        """,
        profile_id,
    )
    return int(row["cnt"]) if row else 0


async def _last_action_at(pool: asyncpg.Pool, profile_id: int) -> datetime | None:
    row = await pool.fetchrow(
        "SELECT MAX(executed_at) AS last FROM ghost_action_log WHERE ghost_profile_id = $1",
        profile_id,
    )
    v = row["last"] if row else None
    if v and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


async def _log_action(
    pool: asyncpg.Pool,
    profile_id: int,
    account_id: int,
    action_type: str,
    target: str | None,
    result: str,
    error_msg: str | None = None,
) -> None:
    await pool.execute(
        """
        INSERT INTO ghost_action_log
               (ghost_profile_id, account_id, action_type, target, result, error_msg)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        profile_id,
        account_id,
        action_type,
        target,
        result,
        error_msg,
    )


# ─── Actions ──────────────────────────────────────────────────────────────────


async def _act_update_status(client, pool, profile_id, account_id) -> None:
    await client(UpdateStatusRequest(offline=False))
    await asyncio.sleep(random.uniform(4, 12))
    await client(UpdateStatusRequest(offline=True))
    await _log_action(pool, profile_id, account_id, "update_status", None, "ok")


async def _act_read_dialogs(client, pool, profile_id, account_id) -> None:
    dialogs = await client.get_dialogs(limit=random.randint(5, 12))
    targets = []
    sample = random.sample(dialogs, min(random.randint(1, 3), len(dialogs)))
    for d in sample:
        try:
            await client.send_read_acknowledge(d.entity)
            t = getattr(d.entity, "title", None) or getattr(d.entity, "username", None)
            if t:
                targets.append(t)
            await asyncio.sleep(random.uniform(1, 4))
        except Exception:
            pass
    await _log_action(pool, profile_id, account_id, "read_dialogs", ",".join(targets[:3]) or None, "ok")


async def _act_react(client, pool, profile_id, account_id) -> None:
    dialogs = await client.get_dialogs(limit=25)
    channels = [
        d for d in dialogs
        if hasattr(d.entity, "broadcast") and d.entity.broadcast
    ]
    if not channels:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", "no_channels")
        return
    ch = random.choice(channels[:15])
    msgs = await client.get_messages(ch.entity, limit=8)
    if not msgs:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", "no_messages")
        return
    msg = random.choice(msgs)
    emoji = random.choice(_SAFE_REACTIONS)
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        await client(SendReactionRequest(
            peer=ch.entity,
            msg_id=msg.id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        target = getattr(ch.entity, "title", None) or getattr(ch.entity, "username", None) or "?"
        await _log_action(pool, profile_id, account_id, "react", target, "ok")
    except (ChatWriteForbiddenError, PeerFloodError) as e:
        await _log_action(pool, profile_id, account_id, "react", None, "skip", str(e)[:120])
    except Exception as e:
        await _log_action(pool, profile_id, account_id, "react", None, "error", str(e)[:200])


async def _act_forward_saved(client, pool, profile_id, account_id) -> None:
    dialogs = await client.get_dialogs(limit=25)
    channels = [
        d for d in dialogs
        if hasattr(d.entity, "broadcast") and d.entity.broadcast
    ]
    if not channels:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "skip", "no_channels")
        return
    ch = random.choice(channels[:15])
    msgs = await client.get_messages(ch.entity, limit=12)
    if not msgs:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "skip", "no_messages")
        return
    msg = random.choice(msgs)
    try:
        await client.forward_messages("me", msg.id, ch.entity)
        target = getattr(ch.entity, "title", None) or getattr(ch.entity, "username", None) or "?"
        await _log_action(pool, profile_id, account_id, "forward_saved", target, "ok")
    except Exception as e:
        await _log_action(pool, profile_id, account_id, "forward_saved", None, "error", str(e)[:200])


_ACTION_POOLS: dict[str, list[str]] = {
    "ghost":   ["update_status", "update_status", "read_dialogs"],
    "watcher": ["update_status", "read_dialogs", "read_dialogs", "react"],
    "active":  ["update_status", "read_dialogs", "react", "react", "forward_saved"],
}

_ACTION_FNS = {
    "update_status": _act_update_status,
    "read_dialogs":  _act_read_dialogs,
    "react":         _act_react,
    "forward_saved": _act_forward_saved,
}


# ─── Profile processing ───────────────────────────────────────────────────────


async def _process_profile(pool: asyncpg.Pool, profile: asyncpg.Record) -> None:
    profile_id = profile["id"]
    account_id = profile["account_id"]
    personality = profile["personality"]

    now = datetime.now(timezone.utc)
    hour = now.hour
    start = profile["active_hours_start"]
    end   = profile["active_hours_end"]
    if start <= end:
        in_window = start <= hour < end
    else:  # wraps midnight
        in_window = hour >= start or hour < end
    if not in_window:
        return

    last = await _last_action_at(pool, profile_id)
    if last:
        elapsed_min = (now - last).total_seconds() / 60
        if elapsed_min < profile["cooldown_minutes"]:
            return

    done_today = await _count_today_actions(pool, profile_id)
    if done_today >= profile["daily_cap"]:
        return

    acc = await pool.fetchrow(
        """
        SELECT id, session_string, session_encrypted, proxy_url, proxy_type,
               proxy_user, proxy_pass, cooldown_until
        FROM tg_accounts
        WHERE id = $1 AND in_operation = FALSE AND banned = FALSE
        """,
        account_id,
    )
    if not acc:
        return

    if acc["cooldown_until"] and acc["cooldown_until"].replace(tzinfo=timezone.utc) > now:
        return

    session = acc["session_string"] or acc["session_encrypted"]
    if not session:
        return

    acc_dict = dict(acc)
    action = random.choice(_ACTION_POOLS.get(personality, _ACTION_POOLS["ghost"]))
    fn = _ACTION_FNS[action]

    try:
        client = await _make_client(session, acc_dict)
        async with client:
            await fn(client, pool, profile_id, account_id)
    except FloodWaitError as e:
        await _log_action(pool, profile_id, account_id, action, None, "skip", f"flood:{e.seconds}s")
    except (UserDeactivatedBanError, AuthKeyError) as e:
        log.warning("Ghost Engine: account %d banned/invalid, disabling profile %d", account_id, profile_id)
        await pool.execute(
            "UPDATE ghost_profiles SET enabled = FALSE, updated_at = NOW() WHERE id = $1",
            profile_id,
        )
        await _log_action(pool, profile_id, account_id, action, None, "error", str(e)[:120])
    except Exception as e:
        log.debug("Ghost Engine: profile %d / account %d error: %s", profile_id, account_id, e)
        await _log_action(pool, profile_id, account_id, action, None, "error", str(e)[:200])


# ─── Main loop ────────────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("Ghost Engine started")
    while True:
        try:
            profiles = await pool.fetch(
                "SELECT * FROM ghost_profiles WHERE enabled = TRUE ORDER BY id"
            )
            for profile in profiles:
                try:
                    await _process_profile(pool, profile)
                except Exception as e:
                    log.debug("Ghost Engine: unhandled error in profile %d: %s", profile["id"], e)
                await asyncio.sleep(random.uniform(_STAGGER_MIN, _STAGGER_MAX))
        except Exception as e:
            log.error("Ghost Engine loop error: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL)
