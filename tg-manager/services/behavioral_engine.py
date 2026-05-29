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

async def run(pool: asyncpg.Pool, bot=None) -> None:
    """Main loop: periodically recompute behavioral scores and detect anomalies."""
    log.info("behavioral_engine started")
    cycle = 0
    while True:
        try:
            await _recompute_scores(pool)
            # Anomaly detection every 12 cycles (~3 hours)
            if cycle % 12 == 0:
                await _detect_anomalies(pool)
            # Auto-pause winning A/B experiments every 4 cycles (~1 hour)
            if cycle % 4 == 0:
                await _auto_conclude_experiments(pool, bot)
            cycle += 1
        except Exception:
            log.exception("behavioral_engine error")
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


# ── Anomaly detection ────────────────────────────────────────────────────

async def _detect_anomalies(pool: asyncpg.Pool) -> None:
    """
    Detect unusual behavioral patterns and log them as 'anomaly' events.
    Currently detects:
    - Sudden decay spike: entity that had high attention_score now has decay_rate > 0.8
    - Search affinity drop: keyword not searched in 14+ days but had affinity > 50
    - Reentry burst: 5+ reentries to same entity in 1 hour (unusual automation signal)
    """
    try:
        # 1. Decay spikes — entities dropping fast
        decay_anomalies = await pool.fetch(
            """SELECT owner_id, entity_type, entity_id, decay_rate, attention_score
               FROM entity_behavioral_score
               WHERE decay_rate > 0.8
                 AND attention_score > 30
                 AND updated_at > now() - INTERVAL '3 hours'""",
        )
        for r in decay_anomalies:
            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, $2, $3, 'anomaly', $4) "
                "ON CONFLICT DO NOTHING",
                r["owner_id"], r["entity_type"], r["entity_id"],
                json.dumps({
                    "type": "decay_spike",
                    "decay_rate": float(r["decay_rate"]),
                    "attention_score": float(r["attention_score"]),
                }),
            )

        # 2. Affinity dropout — keywords gone cold
        cold_keywords = await pool.fetch(
            """SELECT owner_id, keyword, affinity_score, last_searched
               FROM search_memory
               WHERE affinity_score > 50
                 AND last_searched < now() - INTERVAL '14 days'""",
        )
        for r in cold_keywords:
            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, 'keyword', 0, 'anomaly', $2) "
                "ON CONFLICT DO NOTHING",
                r["owner_id"],
                json.dumps({
                    "type": "affinity_dropout",
                    "keyword": r["keyword"],
                    "affinity_score": float(r["affinity_score"]),
                    "days_absent": (datetime.now(tz=timezone.utc) - r["last_searched"]).days,
                }),
            )

        # 3. Reentry burst — more than 5 reentries to same entity in 1 hour
        burst_rows = await pool.fetch(
            """SELECT owner_id, entity_type, entity_id, COUNT(*) AS cnt
               FROM behavioral_events
               WHERE event_type = 'reentry'
                 AND occurred_at > now() - INTERVAL '1 hour'
               GROUP BY owner_id, entity_type, entity_id
               HAVING COUNT(*) >= 5""",
        )
        for r in burst_rows:
            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, $2, $3, 'anomaly', $4)",
                r["owner_id"], r["entity_type"], r["entity_id"],
                json.dumps({"type": "reentry_burst", "count": int(r["cnt"])}),
            )

        if decay_anomalies or cold_keywords or burst_rows:
            log.info(
                "behavioral_engine anomaly scan: %d decay, %d cold kw, %d bursts",
                len(decay_anomalies), len(cold_keywords), len(burst_rows),
            )
    except Exception:
        log.debug("behavioral_engine anomaly detection skipped (table may not exist)")


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


# ── A/B Experiment auto-conclusion ────────────────────────────────────────

import math as _math


def _z_test(n_a: int, c_a: int, n_b: int, c_b: int) -> float:
    """Return z-score for two proportions."""
    if n_a < 5 or n_b < 5 or (c_a + c_b) == 0:
        return 0.0
    p_a = c_a / n_a
    p_b = c_b / n_b
    p_pool = (c_a + c_b) / (n_a + n_b)
    denom = _math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if denom == 0:
        return 0.0
    return abs(p_a - p_b) / denom


async def _auto_conclude_experiments(pool: asyncpg.Pool, bot=None) -> None:
    """
    Check active 2-variant experiments. If z-score >= 1.96 (95% significance)
    AND total impressions >= 200, mark the winner and complete the experiment.
    """
    try:
        exps = await pool.fetch(
            "SELECT id, bot_id FROM ab_experiments WHERE status='active'"
        )
    except Exception:
        return

    concluded = 0
    for exp in exps:
        try:
            variants = await pool.fetch(
                "SELECT id, name, impressions, conversions FROM experiment_variants "
                "WHERE experiment_id=$1 ORDER BY id",
                exp["id"],
            )
            if len(variants) != 2:
                continue
            v0, v1 = variants[0], variants[1]
            n0 = int(v0["impressions"] or 0)
            c0 = int(v0["conversions"] or 0)
            n1 = int(v1["impressions"] or 0)
            c1 = int(v1["conversions"] or 0)
            total = n0 + n1
            if total < 200:
                continue
            z = _z_test(n0, c0, n1, c1)
            if z < 1.96:
                continue

            # Determine winner
            ctr0 = c0 / max(1, n0)
            ctr1 = c1 / max(1, n1)
            winner_id = v0["id"] if ctr0 >= ctr1 else v1["id"]

            await pool.execute(
                "UPDATE ab_experiments SET status='completed', winner_variant_id=$1 WHERE id=$2",
                winner_id, exp["id"],
            )
            concluded += 1
            log.info(
                "behavioral_engine: auto-concluded experiment %d, winner variant %d (z=%.2f)",
                exp["id"], winner_id, z,
            )
            if bot:
                try:
                    owner = await pool.fetchval(
                        "SELECT added_by FROM managed_bots WHERE bot_id=$1", exp["bot_id"]
                    )
                    if owner:
                        await bot.send_message(
                            owner,
                            f"🧪 <b>A/B эксперимент #{exp['id']} завершён</b>\n\n"
                            f"🏆 Победитель: вариант #{winner_id}\n"
                            f"📊 Z-score: {z:.2f} (95% значимость)\n"
                            f"📈 Показов: {total}",
                            parse_mode="HTML",
                        )
                except Exception:
                    pass
        except Exception as exc:
            log.debug("auto_conclude exp %d: %s", exp["id"], exc)

    if concluded:
        log.info("behavioral_engine: auto-concluded %d experiments", concluded)
