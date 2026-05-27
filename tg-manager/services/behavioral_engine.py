"""Behavioral Intelligence Engine.

Background service that:
- Recomputes behavioral scores every 15 minutes
- Provides collector functions called from existing handlers
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import asyncpg

log = logging.getLogger(__name__)

_RESCORE_INTERVAL = 900  # 15 minutes


# ── Background runner ─────────────────────────────────────────────────────

async def run(pool: asyncpg.Pool) -> None:
    """Main loop: periodically recompute behavioral scores."""
    log.info("behavioral_engine started")
    while True:
        try:
            await _recompute_scores(pool)
        except Exception:
            log.exception("behavioral_engine recompute error")
        await asyncio.sleep(_RESCORE_INTERVAL)


# ── Score recomputation ───────────────────────────────────────────────────

async def _recompute_scores(pool: asyncpg.Pool) -> None:
    """Recompute attention/habit/ecosystem scores for all active entities."""
    rows = await pool.fetch(
        """SELECT owner_id, entity_type, entity_id,
                  COUNT(*) AS event_count,
                  MAX(occurred_at) AS last_event,
                  MIN(occurred_at) AS first_event,
                  COUNT(*) FILTER (WHERE event_type = 'reentry') AS reentry_count,
                  COUNT(*) FILTER (WHERE event_type = 'cross_nav') AS cross_nav_count,
                  COUNT(DISTINCT date_trunc('week', occurred_at)) AS active_weeks
           FROM behavioral_events
           WHERE occurred_at > now() - INTERVAL '30 days'
           GROUP BY owner_id, entity_type, entity_id""",
    )
    if not rows:
        return

    now = datetime.now(tz=timezone.utc)
    upserts = []
    for r in rows:
        days_since = max(0.0, (now - r["last_event"]).total_seconds() / 86400)
        recency_bonus = max(0.0, (30 - days_since) * 2)
        attention = min(100.0, r["reentry_count"] * 15 + recency_bonus)
        habit = min(100.0, float(r["active_weeks"]) * 20)
        ecosystem = min(100.0, float(r["cross_nav_count"]) * 25)
        lifespan_days = max(1.0, (r["last_event"] - r["first_event"]).total_seconds() / 86400)
        decay = round(1.0 / (lifespan_days / max(1, r["event_count"])), 4)
        upserts.append((
            r["owner_id"], r["entity_type"], r["entity_id"],
            round(attention, 2), round(habit, 2), round(ecosystem, 2),
            min(1.0, decay), r["reentry_count"],
        ))

    await pool.executemany(
        """INSERT INTO entity_behavioral_score
               (owner_id, entity_type, entity_id,
                attention_score, habit_score, ecosystem_score,
                decay_rate, reentry_count, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
           ON CONFLICT (owner_id, entity_type, entity_id) DO UPDATE
           SET attention_score = EXCLUDED.attention_score,
               habit_score     = EXCLUDED.habit_score,
               ecosystem_score = EXCLUDED.ecosystem_score,
               decay_rate      = EXCLUDED.decay_rate,
               reentry_count   = EXCLUDED.reentry_count,
               updated_at      = now()""",
        upserts,
    )
    log.debug("behavioral_engine: updated %d entity scores", len(upserts))


# ── Collector functions (called from handlers) ────────────────────────────

async def record_reentry(
    pool: asyncpg.Pool,
    owner_id: int,
    entity_type: str,
    entity_id: int,
    days_absent: float = 0.0,
) -> None:
    """Record that a user returned to an entity after absence."""
    try:
        await pool.execute(
            "INSERT INTO behavioral_events"
            "(owner_id, entity_type, entity_id, event_type, meta) "
            "VALUES ($1, $2, $3, 'reentry', $4)",
            owner_id, entity_type, entity_id,
            json.dumps({"days_absent": round(days_absent, 1)}),
        )
    except Exception:
        log.exception("record_reentry error")


async def record_search_repeat(
    pool: asyncpg.Pool,
    owner_id: int,
    keyword: str,
) -> None:
    """Record a repeated search for the same keyword (search affinity)."""
    try:
        await pool.execute(
            """INSERT INTO search_memory(owner_id, keyword, search_count, last_searched)
               VALUES ($1, $2, 1, now())
               ON CONFLICT (owner_id, keyword) DO UPDATE
               SET search_count  = search_memory.search_count + 1,
                   last_searched = now(),
                   affinity_score = LEAST(100, search_memory.affinity_score + 5)""",
            owner_id, keyword,
        )
        await pool.execute(
            "INSERT INTO behavioral_events"
            "(owner_id, entity_type, entity_id, event_type, meta) "
            "VALUES ($1, 'keyword', 0, 'search_repeat', $2)",
            owner_id, json.dumps({"keyword": keyword}),
        )
    except Exception:
        log.exception("record_search_repeat error")


async def record_cross_nav(
    pool: asyncpg.Pool,
    owner_id: int,
    from_type: str,
    from_id: int,
    to_type: str,
    to_id: int,
) -> None:
    """Record navigation between two entity types (ecosystem mapping)."""
    try:
        meta = json.dumps({
            "from_type": from_type, "from_id": from_id,
            "to_type": to_type, "to_id": to_id,
        })
        await pool.execute(
            "INSERT INTO behavioral_events"
            "(owner_id, entity_type, entity_id, event_type, meta) "
            "VALUES ($1, $2, $3, 'cross_nav', $4)",
            owner_id, from_type, from_id, meta,
        )
    except Exception:
        log.exception("record_cross_nav error")


# ── Query helpers (used by dashboard) ────────────────────────────────────

async def get_top_entities(
    pool: asyncpg.Pool,
    owner_id: int,
    score_field: str = "attention_score",
    limit: int = 10,
) -> list[asyncpg.Record]:
    """Return top entities by the given score field."""
    allowed = {"attention_score", "habit_score", "ecosystem_score"}
    if score_field not in allowed:
        score_field = "attention_score"
    return await pool.fetch(
        f"SELECT entity_type, entity_id, attention_score, habit_score, "
        f"ecosystem_score, decay_rate, updated_at "
        f"FROM entity_behavioral_score "
        f"WHERE owner_id=$1 "
        f"ORDER BY {score_field} DESC LIMIT $2",
        owner_id, limit,
    )


async def get_search_memory(
    pool: asyncpg.Pool,
    owner_id: int,
    limit: int = 15,
) -> list[asyncpg.Record]:
    """Return top keywords by affinity score."""
    return await pool.fetch(
        "SELECT keyword, search_count, affinity_score, last_searched "
        "FROM search_memory WHERE owner_id=$1 "
        "ORDER BY affinity_score DESC, search_count DESC LIMIT $2",
        owner_id, limit,
    )
