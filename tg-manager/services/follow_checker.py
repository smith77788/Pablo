"""
Background service: detect and notify changes in followed entities.

Runs every CHECK_INTERVAL_MINUTES. For each followed entity:
  1. Fetch current username/display_name from entity_last_known.
  2. If changed since last_checked_at → record event + notify owner.
  3. Update last_checked_at.

Notification strategy: send Telegram message via Bot API (not Telethon).
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

import asyncpg

CHECK_INTERVAL_MINUTES = 30
MAX_FOLLOWS_PER_ROUND = 200

log = logging.getLogger(__name__)


async def _notify_owner(bot, owner_id: int, entity_id: int, change_type: str,
                         old_username: str | None, new_username: str | None,
                         old_name: str | None, new_name: str | None) -> None:
    entity_ref = f"@{new_username}" if new_username else f"<code>{entity_id}</code>"
    lines = [f"🔔 <b>Изменение у отслеживаемого объекта {entity_ref}</b>\n"]
    if "username" in change_type:
        old_u = f"@{html.escape(old_username)}" if old_username else "без username"
        new_u = f"@{html.escape(new_username)}" if new_username else "без username"
        lines.append(f"🏷 Username: {old_u} → <b>{new_u}</b>")
    if "name" in change_type:
        lines.append(
            f"📝 Имя: {html.escape(old_name or '—')} → <b>{html.escape(new_name or '—')}</b>"
        )
    lines.append(f"\n<i>⏱ Обнаружено: {datetime.now(tz=timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC</i>")
    try:
        await bot.send_message(owner_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        log.debug("follow_checker: notify owner %d failed: %s", owner_id, e)


async def _check_round(pool: asyncpg.Pool, bot) -> None:
    from database import db as _db

    rows = await pool.fetch(
        """SELECT f.id, f.owner_id, f.entity_id, f.entity_type,
                  lk.username   AS cur_username,
                  lk.display_name AS cur_name,
                  -- Last event for this follow (to compare against)
                  (SELECT new_username FROM entity_follow_events
                   WHERE follow_id=f.id ORDER BY detected_at DESC LIMIT 1) AS prev_username,
                  (SELECT new_name FROM entity_follow_events
                   WHERE follow_id=f.id ORDER BY detected_at DESC LIMIT 1) AS prev_name,
                  -- Snapshot at follow-creation if no events yet
                  (SELECT username FROM entity_name_history
                   WHERE entity_id=f.entity_id ORDER BY seen_at ASC LIMIT 1) AS initial_username,
                  (SELECT display_name FROM entity_name_history
                   WHERE entity_id=f.entity_id ORDER BY seen_at ASC LIMIT 1) AS initial_name
           FROM entity_follows f
           LEFT JOIN entity_last_known lk ON lk.entity_id = f.entity_id
           ORDER BY f.last_checked_at ASC NULLS FIRST
           LIMIT $1""",
        MAX_FOLLOWS_PER_ROUND,
    )

    event_batch: list[int] = []
    for row in rows:
        cur_u = row["cur_username"]
        cur_n = row["cur_name"]
        # Use last event as baseline; fall back to initial snapshot
        base_u = row["prev_username"] if row["prev_username"] is not None else row["initial_username"]
        base_n = row["prev_name"] if row["prev_name"] is not None else row["initial_name"]

        u_changed = cur_u != base_u
        n_changed = cur_n != base_n

        if u_changed or n_changed:
            if u_changed and n_changed:
                change_type = "both_changed"
            elif u_changed:
                change_type = "username_changed"
            else:
                change_type = "name_changed"

            event_id = await _db.record_follow_change(
                pool,
                follow_id=row["id"],
                owner_id=row["owner_id"],
                entity_id=row["entity_id"],
                change_type=change_type,
                old_username=base_u,
                new_username=cur_u,
                old_name=base_n,
                new_name=cur_n,
            )
            if event_id:
                await _notify_owner(
                    bot, row["owner_id"], row["entity_id"], change_type,
                    base_u, cur_u, base_n, cur_n,
                )
                event_batch.append(event_id)

        # Update last_checked_at
        try:
            await pool.execute(
                "UPDATE entity_follows SET last_checked_at=NOW() WHERE id=$1",
                row["id"],
            )
        except Exception:
            pass

    if event_batch:
        await _db.mark_follow_notifications_sent(pool, event_batch)
        log.info("follow_checker: detected %d changes, notified owners", len(event_batch))


async def run_follow_checker(pool: asyncpg.Pool, bot) -> None:
    """Background loop. Call once from main.py as asyncio.create_task()."""
    log.info("follow_checker: started, interval=%dm", CHECK_INTERVAL_MINUTES)
    while True:
        try:
            await _check_round(pool, bot)
        except Exception as e:
            log.warning("follow_checker: error in round: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)
