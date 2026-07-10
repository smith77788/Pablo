"""Search Ranking Engine — real-time position tracking for Telegram search.

Tracks channel/bot positions in Telegram search results over time,
provides historical data, alerts on position changes, and suggests
optimization actions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# How often to check positions (seconds)
CHECK_INTERVAL = 3600  # 1 hour
# Max history retention (days)
HISTORY_RETENTION = 90
# Alert threshold: position dropped by more than N
ALERT_THRESHOLD = 10


@dataclass
class PositionSnapshot:
    channel_id: int
    keyword: str
    position: int
    checked_at: datetime
    previous_position: Optional[int] = None


async def init_ranking_tables(pool: asyncpg.Pool) -> None:
    """Create ranking tables if they don't exist."""
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS search_rankings (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            keyword TEXT NOT NULL,
            position INTEGER,
            previous_position INTEGER,
            checked_at TIMESTAMPTZ DEFAULT NOW(),
            metadata JSONB DEFAULT '{}'
        );
    ''')
    await pool.execute('''
        CREATE INDEX IF NOT EXISTS idx_search_rankings_owner
        ON search_rankings(owner_id, channel_id, keyword, checked_at DESC);
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS tracked_keywords (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            keyword TEXT NOT NULL,
            channel_id BIGINT,
            is_active BOOLEAN DEFAULT TRUE,
            check_interval INTEGER DEFAULT 3600,
            last_checked_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(owner_id, keyword, channel_id)
        );
    ''')
    await pool.execute('''
        CREATE TABLE IF NOT EXISTS ranking_alerts (
            id SERIAL PRIMARY KEY,
            owner_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            keyword TEXT NOT NULL,
            old_position INTEGER,
            new_position INTEGER,
            alert_type TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            acknowledged BOOLEAN DEFAULT FALSE
        );
    ''')
    log.info("Ranking tables initialized")


async def track_keyword(pool: asyncpg.Pool, owner_id: int, keyword: str,
                        channel_id: Optional[int] = None,
                        check_interval: int = 3600) -> dict:
    """Start tracking a keyword for position monitoring."""
    try:
        row = await pool.fetchrow(
            '''INSERT INTO tracked_keywords (owner_id, keyword, channel_id, check_interval)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (owner_id, keyword, channel_id) DO UPDATE
               SET is_active = TRUE, check_interval = $4
               RETURNING id''',
            owner_id, keyword, channel_id, check_interval)
        return {'ok': True, 'id': row['id']}
    except Exception as e:
        log.warning("track_keyword error: %s", e)
        return {'ok': False, 'error': str(e)}


async def untrack_keyword(pool: asyncpg.Pool, owner_id: int, keyword: str,
                          channel_id: Optional[int] = None) -> dict:
    """Stop tracking a keyword."""
    try:
        await pool.execute(
            '''UPDATE tracked_keywords SET is_active = FALSE
               WHERE owner_id = $1 AND keyword = $2
               AND (channel_id = $3 OR ($3 IS NULL AND channel_id IS NULL))''',
            owner_id, keyword, channel_id)
        return {'ok': True}
    except Exception as e:
        log.warning("untrack_keyword error: %s", e)
        return {'ok': False, 'error': str(e)}


async def record_position(pool: asyncpg.Pool, owner_id: int, channel_id: int,
                          keyword: str, position: int) -> dict:
    """Record a position check result."""
    try:
        # Get previous position
        prev = await pool.fetchval(
            '''SELECT position FROM search_rankings
               WHERE owner_id = $1 AND channel_id = $2 AND keyword = $3
               ORDER BY checked_at DESC LIMIT 1''',
            owner_id, channel_id, keyword)

        await pool.execute(
            '''INSERT INTO search_rankings (owner_id, channel_id, keyword, position, previous_position)
               VALUES ($1, $2, $3, $4, $5)''',
            owner_id, channel_id, keyword, position, prev)

        # Update last_checked_at on tracked keyword
        await pool.execute(
            '''UPDATE tracked_keywords SET last_checked_at = NOW()
               WHERE owner_id = $1 AND keyword = $2 AND channel_id = $3''',
            owner_id, keyword, channel_id)

        # Check for alerts
        if prev and position > prev + ALERT_THRESHOLD:
            await _create_alert(pool, owner_id, channel_id, keyword, prev, position, 'dropped')
        elif prev and position < prev - ALERT_THRESHOLD:
            await _create_alert(pool, owner_id, channel_id, keyword, prev, position, 'improved')

        return {'ok': True, 'previous': prev, 'current': position}
    except Exception as e:
        log.warning("record_position error: %s", e)
        return {'ok': False, 'error': str(e)}


async def _create_alert(pool: asyncpg.Pool, owner_id: int, channel_id: int,
                        keyword: str, old_pos: int, new_pos: int, alert_type: str) -> None:
    """Create a ranking alert."""
    try:
        await pool.execute(
            '''INSERT INTO ranking_alerts (owner_id, channel_id, keyword, old_position, new_position, alert_type)
               VALUES ($1, $2, $3, $4, $5, $6)''',
            owner_id, channel_id, keyword, old_pos, new_pos, alert_type)
    except Exception as e:
        log.warning("create_alert error: %s", e)


async def get_position_history(pool: asyncpg.Pool, owner_id: int,
                               channel_id: int, keyword: str,
                               days: int = 30) -> list:
    """Get position history for a channel/keyword."""
    try:
        rows = await pool.fetch(
            '''SELECT position, previous_position, checked_at, metadata
               FROM search_rankings
               WHERE owner_id = $1 AND channel_id = $2 AND keyword = $3
               AND checked_at > NOW() - ($4 || ' days')::INTERVAL
               ORDER BY checked_at ASC''',
            owner_id, channel_id, keyword, str(days))
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_position_history error: %s", e)
        return []


async def get_all_positions(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get latest position for all tracked keywords."""
    try:
        rows = await pool.fetch(
            '''SELECT DISTINCT ON (channel_id, keyword)
               channel_id, keyword, position, previous_position, checked_at
               FROM search_rankings
               WHERE owner_id = $1
               ORDER BY channel_id, keyword, checked_at DESC''',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_all_positions error: %s", e)
        return []


async def get_tracked_keywords(pool: asyncpg.Pool, owner_id: int) -> list:
    """Get all tracked keywords."""
    try:
        rows = await pool.fetch(
            '''SELECT id, keyword, channel_id, is_active, check_interval, last_checked_at, created_at
               FROM tracked_keywords
               WHERE owner_id = $1
               ORDER BY keyword''',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_tracked_keywords error: %s", e)
        return []


async def get_alerts(pool: asyncpg.Pool, owner_id: int,
                     unacknowledged_only: bool = False) -> list:
    """Get ranking alerts."""
    try:
        where = "owner_id = $1"
        if unacknowledged_only:
            where += " AND acknowledged = FALSE"
        rows = await pool.fetch(
            f'''SELECT id, channel_id, keyword, old_position, new_position,
                       alert_type, created_at, acknowledged
                FROM ranking_alerts
                WHERE {where}
                ORDER BY created_at DESC LIMIT 50''',
            owner_id)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("get_alerts error: %s", e)
        return []


async def acknowledge_alert(pool: asyncpg.Pool, owner_id: int, alert_id: int) -> dict:
    """Acknowledge a ranking alert."""
    try:
        await pool.execute(
            'UPDATE ranking_alerts SET acknowledged = TRUE WHERE id = $1 AND owner_id = $2',
            alert_id, owner_id)
        return {'ok': True}
    except Exception as e:
        log.warning("acknowledge_alert error: %s", e)
        return {'ok': False, 'error': str(e)}


async def get_ranking_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Get ranking statistics."""
    try:
        total_tracked = await pool.fetchval(
            'SELECT COUNT(*) FROM tracked_keywords WHERE owner_id = $1 AND is_active = TRUE',
            owner_id)
        total_checks = await pool.fetchval(
            'SELECT COUNT(*) FROM search_rankings WHERE owner_id = $1', owner_id)
        avg_position = await pool.fetchval(
            '''SELECT AVG(position) FROM search_rankings sr
               WHERE sr.owner_id = $1 AND sr.checked_at > NOW() - INTERVAL '7 days' ''',
            owner_id)
        alerts_pending = await pool.fetchval(
            'SELECT COUNT(*) FROM ranking_alerts WHERE owner_id = $1 AND acknowledged = FALSE',
            owner_id)
        return {
            'total_tracked': total_tracked or 0,
            'total_checks': total_checks or 0,
            'avg_position_7d': round(float(avg_position or 0), 1),
            'alerts_pending': alerts_pending or 0,
        }
    except Exception as e:
        log.warning("get_ranking_stats error: %s", e)
        return {'total_tracked': 0, 'total_checks': 0, 'avg_position_7d': 0, 'alerts_pending': 0}


async def cleanup_old_data(pool: asyncpg.Pool, days: int = HISTORY_RETENTION) -> int:
    """Remove old ranking data beyond retention period."""
    try:
        result = await pool.execute(
            '''DELETE FROM search_rankings
               WHERE checked_at < NOW() - ($1 || ' days')::INTERVAL''',
            str(days))
        count = int(result.split()[-1]) if result.startswith('DELETE') else 0
        if count > 0:
            log.info("Cleaned up %d old ranking records", count)
        return count
    except Exception as e:
        log.warning("cleanup_old_data error: %s", e)
        return 0
