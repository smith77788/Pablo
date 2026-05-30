"""
Drift Detector — периодически проверяет managed_channels на изменения
title / username / about и записывает дрейф в restriction_events.
"""
import asyncio
import json
import logging

import asyncpg

from database import db
from services import account_manager

log = logging.getLogger(__name__)

_INTERVAL = 4 * 3600          # 4 часа между полными сканами
_BATCH_SIZE = 10               # каналов за одну сессию аккаунта
_PAUSE_BETWEEN = 3.0           # секунд между запросами к одному аккаунту


async def run(pool: asyncpg.Pool, bot) -> None:
    while True:
        try:
            await _check_all(pool, bot)
        except Exception:
            log.exception("drift_detector cycle error")
        await asyncio.sleep(_INTERVAL)


async def _check_all(pool: asyncpg.Pool, bot) -> None:
    # Channels not checked in last 3 hours (give margin before 4h interval)
    rows = await pool.fetch(
        """SELECT mc.id, mc.owner_id, mc.acc_id, mc.channel_id,
                  mc.title, mc.username, mc.about, mc.access_hash
           FROM managed_channels mc
           WHERE mc.last_drift_check IS NULL
              OR mc.last_drift_check < now() - INTERVAL '3 hours'
           ORDER BY mc.owner_id, mc.acc_id
           LIMIT 200"""
    )
    if not rows:
        log.debug("drift_detector: nothing to check")
        return

    log.info("drift_detector: checking %d channels", len(rows))

    # Group by (owner_id, acc_id) to minimise sessions opened
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["owner_id"], r["acc_id"])
        groups.setdefault(key, []).append(r)

    for (owner_id, acc_id), channels in groups.items():
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND is_active=true", acc_id
        )
        if not acc:
            continue
        acc_dict = dict(acc)

        for ch in channels[:_BATCH_SIZE]:
            try:
                info = await account_manager.get_full_channel_info(
                    acc_dict["session_str"], ch["channel_id"], _acc=acc_dict
                )
            except Exception as e:
                log.debug("drift_detector get_full_channel_info error: %s", e)
                info = None

            # Always update last_drift_check
            await pool.execute(
                "UPDATE managed_channels SET last_drift_check=now() WHERE id=$1",
                ch["id"],
            )

            if not info:
                await asyncio.sleep(1.0)
                continue

            new_title    = (info.get("title") or "").strip()
            new_username = (info.get("username") or "").strip()
            new_about    = (info.get("about") or "").strip()

            old_title    = (ch["title"] or "").strip()
            old_username = (ch["username"] or "").strip()
            old_about    = (ch["about"] or "").strip()

            changes: dict = {}
            if old_title and new_title and new_title != old_title:
                changes["title"] = {"old": old_title, "new": new_title}
            if old_username and new_username and new_username != old_username:
                changes["username"] = {"old": old_username, "new": new_username}
            if old_about and new_about and new_about != old_about:
                changes["about"] = {
                    "old": old_about[:200],
                    "new": new_about[:200],
                }

            if changes:
                log.info(
                    "drift_detector: channel %d changed — %s",
                    ch["channel_id"], list(changes),
                )
                severity = "warning" if len(changes) > 1 else "info"
                await pool.execute(
                    "INSERT INTO restriction_events"
                    "(owner_id, event_type, severity, details) "
                    "VALUES ($1, 'drift_detected', $2, $3)",
                    owner_id, severity,
                    json.dumps({
                        "channel_id": ch["channel_id"],
                        "channel_title": old_title or new_title,
                        "changes": changes,
                    }),
                )
                # Update stored values
                await pool.execute(
                    "UPDATE managed_channels "
                    "SET title=$2, username=$3, about=$4 WHERE id=$1",
                    ch["id"], new_title or old_title, new_username or old_username, new_about,
                )
                # Notify owner (uses 'restriction' preference)
                ch_name = old_title or new_title or f"#{ch['channel_id']}"
                change_lines = []
                for field, diff in changes.items():
                    change_lines.append(
                        f"  <b>{field}</b>: «{diff['old']}» → «{diff['new']}»"
                    )
                msg = (
                    f"⚠️ <b>Дрейф канала</b>\n\n"
                    f"<b>{ch_name}</b> изменился:\n"
                    + "\n".join(change_lines)
                )
                await db.notify_if_enabled(pool, bot, owner_id, "restriction", msg)
            elif not old_about and new_about:
                # First-time about capture — just store it silently
                await pool.execute(
                    "UPDATE managed_channels SET about=$2 WHERE id=$1",
                    ch["id"], new_about,
                )

            await asyncio.sleep(_PAUSE_BETWEEN)
