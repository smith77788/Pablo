"""Content Mesh — automated content distribution from source channels to targets.

Each mesh watches a source Telegram channel for new posts and redistributes
them (as reposts, not forwards) to all enabled target channels with a
configurable delay.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from aiogram import Bot
from telethon.errors import (
    AuthKeyError,
    ChannelPrivateError,
    ChatWriteForbiddenError,
    FloodWaitError,
    UserDeactivatedBanError,
    UserNotParticipantError,
)

from services.account_manager import _make_client

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 120    # seconds between full sweeps
_MAX_NEW_PER_CYCLE = 10 # max new source messages to enqueue per cycle per mesh


# ─── Source polling ───────────────────────────────────────────────────────────


async def _poll_source(pool: asyncpg.Pool, mesh: asyncpg.Record) -> None:
    """Check source channel for new messages and enqueue them for targets."""
    mesh_id = mesh["id"]
    source_channel = mesh["source_channel"]
    account_id = mesh["source_account_id"]
    last_id = mesh["last_post_id"] or 0
    delay = mesh["delay_minutes"]

    acc = await pool.fetchrow(
        """
        SELECT a.id, a.session_str, a.cooldown_until, a.banned,
               a.device_model, a.system_version, a.app_version,
               a.lang_code, a.system_lang_code,
               p.proxy_url
        FROM tg_accounts a
        LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
        WHERE a.id = $1 AND a.banned = FALSE
        """,
        account_id,
    )
    if not acc:
        return

    session = acc["session_str"]
    if not session:
        return

    targets = await pool.fetch(
        "SELECT * FROM mesh_targets WHERE mesh_id=$1 AND enabled=TRUE",
        mesh_id,
    )
    if not targets:
        return

    acc_dict = dict(acc)
    try:
        client = _make_client(session, acc_dict)
        async with client:
            entity = await client.get_entity(source_channel)
            messages = await client.get_messages(entity, limit=_MAX_NEW_PER_CYCLE, min_id=last_id)

            if not messages:
                return

            new_max_id = last_id
            now = datetime.now(timezone.utc)

            for msg in reversed(messages):  # oldest first
                if msg.id <= last_id:
                    continue
                new_max_id = max(new_max_id, msg.id)

                # Skip service messages (no text, no media)
                if not msg.text and not msg.media:
                    continue

                # Enqueue delivery to each target
                scheduled = now + timedelta(minutes=delay)
                for target in targets:
                    existing = await pool.fetchrow(
                        "SELECT id FROM mesh_queue WHERE mesh_id=$1 AND target_id=$2 AND source_msg_id=$3",
                        mesh_id, target["id"], msg.id,
                    )
                    if existing:
                        continue
                    await pool.execute(
                        """
                        INSERT INTO mesh_queue (mesh_id, target_id, source_msg_id, scheduled_at)
                        VALUES ($1, $2, $3, $4)
                        """,
                        mesh_id, target["id"], msg.id, scheduled,
                    )

            if new_max_id > last_id:
                await pool.execute(
                    "UPDATE content_meshes SET last_post_id=$1, updated_at=NOW() WHERE id=$2",
                    new_max_id, mesh_id,
                )

    except FloodWaitError as e:
        log.debug("Content Mesh: flood wait %ds for mesh %d source poll", e.seconds, mesh_id)
    except (UserDeactivatedBanError, AuthKeyError):
        log.warning("Content Mesh: account %d banned, disabling mesh %d", account_id, mesh_id)
        await pool.execute("UPDATE content_meshes SET enabled=FALSE WHERE id=$1", mesh_id)
    except (ChannelPrivateError, UserNotParticipantError) as e:
        log.debug("Content Mesh: can't access source %s for mesh %d: %s", source_channel, mesh_id, e)
    except Exception as e:
        log.debug("Content Mesh: source poll error for mesh %d: %s", mesh_id, e)


# ─── Queue processing ─────────────────────────────────────────────────────────


async def _process_delivery(pool: asyncpg.Pool, item: asyncpg.Record) -> None:
    """Send one queued message to its target."""
    mesh = await pool.fetchrow(
        "SELECT * FROM content_meshes WHERE id=$1", item["mesh_id"]
    )
    if not mesh or not mesh["enabled"]:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='mesh_disabled' WHERE id=$1", item["id"])
        return

    target = await pool.fetchrow(
        "SELECT * FROM mesh_targets WHERE id=$1 AND enabled=TRUE", item["target_id"]
    )
    if not target:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='target_disabled' WHERE id=$1", item["id"])
        return

    account_id = mesh["source_account_id"]
    acc = await pool.fetchrow(
        """SELECT a.id, a.session_str, a.cooldown_until, a.banned,
                  a.device_model, a.system_version, a.app_version,
                  a.lang_code, a.system_lang_code,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.id = $1 AND a.banned = FALSE""",
        account_id,
    )
    if not acc:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='no_account' WHERE id=$1", item["id"])
        return

    session = acc["session_str"]
    if not session:
        await pool.execute("UPDATE mesh_queue SET status='error', error_msg='no_session' WHERE id=$1", item["id"])
        return

    try:
        client = _make_client(session, dict(acc))
        async with client:
            source_entity = await client.get_entity(mesh["source_channel"])
            source_msgs = await client.get_messages(source_entity, ids=item["source_msg_id"])
            if not source_msgs:
                await pool.execute(
                    "UPDATE mesh_queue SET status='error', error_msg='source_msg_not_found', sent_at=NOW() WHERE id=$1",
                    item["id"],
                )
                return

            msg = source_msgs if not isinstance(source_msgs, list) else source_msgs[0]
            if not msg:
                await pool.execute(
                    "UPDATE mesh_queue SET status='error', error_msg='source_msg_none', sent_at=NOW() WHERE id=$1",
                    item["id"],
                )
                return

            target_entity = await client.get_entity(target["target_channel"])

            # Build text with optional CTA
            text = msg.text or ""
            if mesh["append_text"]:
                text = (text + "\n\n" + mesh["append_text"]).strip()

            if msg.media and not text:
                await client.send_file(target_entity, msg.media)
            elif msg.media:
                await client.send_file(target_entity, msg.media, caption=text)
            else:
                await client.send_message(target_entity, text)

        await pool.execute(
            "UPDATE mesh_queue SET status='sent', sent_at=NOW() WHERE id=$1", item["id"]
        )

    except FloodWaitError as e:
        # Reschedule rather than fail
        new_time = datetime.now(timezone.utc) + timedelta(seconds=e.seconds + 30)
        await pool.execute(
            "UPDATE mesh_queue SET scheduled_at=$1 WHERE id=$2",
            new_time, item["id"],
        )
    except (ChatWriteForbiddenError, ChannelPrivateError) as e:
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg=$1, sent_at=NOW() WHERE id=$2",
            str(e)[:200], item["id"],
        )
        await pool.execute(
            "UPDATE mesh_targets SET enabled=FALSE WHERE id=$1", item["target_id"]
        )
    except (UserDeactivatedBanError, AuthKeyError):
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg='account_banned', sent_at=NOW() WHERE id=$1",
            item["id"],
        )
        await pool.execute("UPDATE content_meshes SET enabled=FALSE WHERE id=$1", item["mesh_id"])
    except Exception as e:
        await pool.execute(
            "UPDATE mesh_queue SET status='error', error_msg=$1, sent_at=NOW() WHERE id=$2",
            str(e)[:200], item["id"],
        )


# ─── Main loop ────────────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    log.info("Content Mesh started")
    while True:
        try:
            # Poll sources for new content
            meshes = await pool.fetch(
                """
                SELECT * FROM content_meshes
                WHERE enabled = TRUE
                  AND source_channel IS NOT NULL
                  AND source_account_id IS NOT NULL
                ORDER BY id
                """
            )
            for mesh in meshes:
                try:
                    await _poll_source(pool, mesh)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("Content Mesh: poll error mesh %d: %s", mesh["id"], e)
                await asyncio.sleep(5)

            # Process pending deliveries
            pending = await pool.fetch(
                """
                SELECT * FROM mesh_queue
                WHERE status = 'pending' AND scheduled_at <= NOW()
                ORDER BY scheduled_at
                LIMIT 50
                """
            )
            for item in pending:
                try:
                    await _process_delivery(pool, item)
                except Exception as e:
                    log.debug("Content Mesh: delivery error item %d: %s", item["id"], e)
                await asyncio.sleep(3)

        except Exception as e:
            log.error("Content Mesh loop error: %s", e)

        await asyncio.sleep(_LOOP_INTERVAL)
