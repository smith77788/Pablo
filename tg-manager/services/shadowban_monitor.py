"""Background service: detect Telegram shadowban and account restrictions.

Checks:
1. Search visibility drop — bot disappears from search rankings (was visible, now not)
2. Account flood rate — accounts with high flood_count_7d flagged as risk
3. Search position collapse — position drops > 10 places vs 7-day average
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg
from aiogram import Bot

from database.db import notify_if_enabled

log = logging.getLogger(__name__)

_INTERVAL = 1800  # check every 30 minutes
_ALERT_COOLDOWN_HOURS = 24  # don't re-alert same event within 24h
_FLOOD_THRESHOLD = 3  # accounts with flood_count_7d >= this are high-risk
_POSITION_DROP_THRESHOLD = 10  # position drop > this = alert


async def _is_on_cooldown(
    pool: asyncpg.Pool,
    owner_id: int,
    event_type: str,
    entity_id: int = 0,
) -> bool:
    row = await pool.fetchrow(
        """SELECT last_alerted FROM restriction_alert_cooldown
           WHERE owner_id=$1 AND event_type=$2 AND entity_id=$3""",
        owner_id,
        event_type,
        entity_id,
    )
    if not row:
        return False
    from datetime import timezone
    import datetime

    last = row["last_alerted"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    delta = datetime.datetime.now(timezone.utc) - last
    return delta.total_seconds() < _ALERT_COOLDOWN_HOURS * 3600


async def _mark_alerted(
    pool: asyncpg.Pool,
    owner_id: int,
    event_type: str,
    entity_id: int = 0,
) -> None:
    await pool.execute(
        """INSERT INTO restriction_alert_cooldown(owner_id, event_type, entity_id, last_alerted)
           VALUES($1, $2, $3, NOW())
           ON CONFLICT(owner_id, event_type, entity_id)
           DO UPDATE SET last_alerted=NOW()""",
        owner_id,
        event_type,
        entity_id,
    )


async def _record_event(
    pool: asyncpg.Pool,
    owner_id: int,
    event_type: str,
    severity: str,
    details: dict,
    account_id: int | None = None,
    bot_id: int | None = None,
) -> None:
    await pool.execute(
        """INSERT INTO restriction_events(owner_id, account_id, bot_id, event_type, severity, details, alerted_at)
           VALUES($1, $2, $3, $4, $5, $6::jsonb, NOW())""",
        owner_id,
        account_id,
        bot_id,
        event_type,
        severity,
        json.dumps(details, ensure_ascii=False),
    )


async def _check_search_visibility(pool: asyncpg.Pool, bot: Bot) -> None:
    """Detect bots that disappeared from search (had rank, now not found)."""
    # Find bots that had a position in the last 7 days but NOT in the last 3 checks
    rows = await pool.fetch(
        """
        WITH recent AS (
            SELECT DISTINCT ON (bot_id) bot_id, position, checked_at
            FROM search_rankings
            ORDER BY bot_id, checked_at DESC
        ),
        historical AS (
            SELECT bot_id, AVG(position) AS avg_position
            FROM search_rankings
            WHERE checked_at > NOW() - INTERVAL '7 days'
              AND position IS NOT NULL
            GROUP BY bot_id
        )
        SELECT r.bot_id, r.position AS last_position,
               h.avg_position,
               mb.added_by AS owner_id, mb.username
        FROM recent r
        JOIN historical h ON h.bot_id = r.bot_id
        JOIN managed_bots mb ON mb.bot_id = r.bot_id
        WHERE h.avg_position IS NOT NULL
          AND (
              -- Bot disappeared: was ranked, now not found
              (r.position IS NULL AND h.avg_position <= 20)
              OR
              -- Position collapsed: dropped > threshold
              (r.position IS NOT NULL AND h.avg_position IS NOT NULL
               AND r.position - h.avg_position > $1)
          )
        """,
        _POSITION_DROP_THRESHOLD,
    )

    for row in rows:
        owner_id = row["owner_id"]
        bot_id = row["bot_id"]
        last_pos = row["last_position"]
        avg_pos = row["avg_position"]

        if last_pos is None:
            event_type = "search_drop"
            severity = "critical"
            message = (
                f"🚨 <b>Бот пропал из поиска!</b>\n\n"
                f"Бот @{row['username'] or bot_id} больше не отображается в результатах поиска Telegram.\n"
                f"Средняя позиция за 7 дней была: <b>{avg_pos:.0f}</b>\n\n"
                f"Возможная причина: shadowban или ограничение аккаунта."
            )
        else:
            event_type = "search_position_drop"
            severity = "warning"
            drop = int(row["last_position"] - avg_pos)
            message = (
                f"⚠️ <b>Резкое падение позиции в поиске</b>\n\n"
                f"Бот @{row['username'] or bot_id}: позиция упала с <b>{avg_pos:.0f}</b> до <b>{last_pos}</b> "
                f"(−{drop} позиций).\n\n"
                f"Рекомендуется проверить ключевые слова и SEO-описание."
            )

        if await _is_on_cooldown(pool, owner_id, event_type, bot_id):
            continue

        await _record_event(
            pool,
            owner_id,
            event_type,
            severity,
            {
                "bot_id": bot_id,
                "last_position": last_pos,
                "avg_position": float(avg_pos or 0),
            },
            bot_id=bot_id,
        )
        try:
            await notify_if_enabled(pool, bot, owner_id, "restriction", message)
            await _mark_alerted(pool, owner_id, event_type, bot_id)
        except Exception as exc:
            log.warning(
                "shadowban_monitor: failed to alert owner=%s: %s", owner_id, exc
            )


async def _check_account_restrictions(pool: asyncpg.Pool, bot: Bot) -> None:
    """Detect accounts with high flood rate without claiming a verified restriction."""
    rows = await pool.fetch(
        """SELECT ta.id, ta.owner_id, ta.phone, ta.flood_count_7d, ta.last_flood_at
           FROM tg_accounts ta
           WHERE ta.is_active = true
             AND ta.flood_count_7d >= $1""",
        _FLOOD_THRESHOLD,
    )

    for row in rows:
        owner_id = row["owner_id"]
        account_id = row["id"]

        event_type = "account_flood_risk"
        if await _is_on_cooldown(pool, owner_id, event_type, account_id):
            continue

        severity = "critical" if row["flood_count_7d"] >= 5 else "warning"
        message = (
            f"{'🚨' if severity == 'critical' else '⚠️'} <b>Высокий риск лимитов аккаунта</b>\n\n"
            f"Аккаунт <code>{row['phone']}</code> получил "
            f"<b>{row['flood_count_7d']}</b> FloodWait за последние 7 дней.\n\n"
            f"Это не подтверждённый спамблок. Система снизит нагрузку на аккаунт "
            f"и будет ждать явной проверки через @SpamBot для статуса ограничения."
        )

        await _record_event(
            pool,
            owner_id,
            event_type,
            severity,
            {"account_id": account_id, "flood_count_7d": row["flood_count_7d"]},
            account_id=account_id,
        )
        try:
            await notify_if_enabled(pool, bot, owner_id, "restriction", message)
            await _mark_alerted(pool, owner_id, event_type, account_id)
        except Exception as exc:
            log.warning(
                "shadowban_monitor: failed to alert owner=%s: %s", owner_id, exc
            )


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background loop."""
    await asyncio.sleep(180)  # startup delay 3 min
    while True:
        try:
            await _check_search_visibility(pool, bot)
            await _check_account_restrictions(pool, bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("shadowban_monitor error: %s", exc)
        await asyncio.sleep(_INTERVAL)
