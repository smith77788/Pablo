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

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_RESCORE_INTERVAL = 900  # 15 minutes


# ── Background runner ─────────────────────────────────────────────────────


_PRUNE_INTERVAL_CYCLES = 96  # prune once every 96 × 15min ≈ 24 hours
_PRUNE_RETAIN_DAYS = 60       # keep 60 days of events (2 months of history)


async def run(pool: asyncpg.Pool, bot=None) -> None:
    """Main loop: periodically recompute behavioral scores and detect anomalies.

    Uses time-anchored sleep to prevent cycle overlap: if a cycle takes longer
    than _RESCORE_INTERVAL the next sleep is 0 rather than stacking up.
    """
    log.info("behavioral_engine started")
    cycle = 0
    while True:
        started_at = asyncio.get_event_loop().time()
        try:
            await _recompute_scores(pool)
            # Anomaly detection every 12 cycles (~3 hours)
            if cycle % 12 == 0:
                await _detect_anomalies(pool)
            # Auto-pause winning A/B experiments every 4 cycles (~1 hour)
            if cycle % 4 == 0:
                await _auto_conclude_experiments(pool, bot)
            # Prune old behavioral_events once per day to prevent unbounded growth.
            # _recompute_scores only looks back 30 days; we keep 60 days for anomaly
            # detection headroom. Beyond that, rows are dead weight.
            if cycle % _PRUNE_INTERVAL_CYCLES == 0 and cycle > 0:
                await _prune_old_events(pool)
            cycle += 1
        except Exception:
            log.exception("behavioral_engine error")
        elapsed = asyncio.get_event_loop().time() - started_at
        await asyncio.sleep(max(0.0, _RESCORE_INTERVAL - elapsed))


# ── Event pruning ────────────────────────────────────────────────────────


async def _prune_old_events(pool: asyncpg.Pool) -> None:
    """Delete behavioral_events older than _PRUNE_RETAIN_DAYS.

    Without pruning, a platform with 50 active users each generating
    10–30 events/day accumulates 100k–500k rows/month, reaching tens of
    millions within 6 months. _recompute_scores already queries only the
    last 30 days, so anything older than 60 days is never read again.

    Deletes in batches to avoid a long-held lock on the table.
    """
    total_deleted = 0
    batch_size = 5000
    while True:
        try:
            result = await pool.execute(
                """DELETE FROM behavioral_events
                   WHERE id IN (
                       SELECT id FROM behavioral_events
                       WHERE occurred_at < now() - ($1 * INTERVAL '1 day')
                       LIMIT $2
                   )""",
                _PRUNE_RETAIN_DAYS,
                batch_size,
            )
            deleted = int(str(result).split()[-1])
            total_deleted += deleted
            if deleted < batch_size:
                break
            # Yield between batches so other queries are not blocked
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("behavioral_engine: prune error: %s", e)
            break
    if total_deleted:
        log.info(
            "behavioral_engine: pruned %d behavioral_events older than %d days",
            total_deleted,
            _PRUNE_RETAIN_DAYS,
        )


# ── Score recomputation ───────────────────────────────────────────────────


async def _recompute_scores(pool: asyncpg.Pool) -> None:
    """Recompute attention/habit/ecosystem scores for all active entities.

    Scoring uses logarithmic scaling to avoid linear runaway:
      - attention: weighted reentry + recency bonus (log-scaled reentry count)
      - habit: active_weeks × consistency_factor (standard deviation penalty)
      - ecosystem: cross_nav_count × diversity_bonus (unique destination types)
    """
    rows = await pool.fetch(
        """SELECT owner_id, entity_type, entity_id,
                  COUNT(*) AS event_count,
                  MAX(occurred_at) AS last_event,
                  MIN(occurred_at) AS first_event,
                  COUNT(*) FILTER (WHERE event_type = 'reentry') AS reentry_count,
                  COUNT(*) FILTER (WHERE event_type = 'cross_nav') AS cross_nav_count,
                  COUNT(DISTINCT date_trunc('week', occurred_at)) AS active_weeks,
                  ROUND(STDDEV(
                      EXTRACT(DOW FROM occurred_at) * 24 + EXTRACT(HOUR FROM occurred_at)
                  )::numeric, 1) AS time_stddev_hours
           FROM behavioral_events
           WHERE occurred_at > now() - INTERVAL '30 days'
           GROUP BY owner_id, entity_type, entity_id""",
    )
    if not rows:
        return

    import math

    now = datetime.now(tz=timezone.utc)
    upserts = []
    for r in rows:
        days_since = max(0.0, (now - r["last_event"]).total_seconds() / 86400)

        # ── Fine-tuned attention score ──
        # Logarithmic scaling for reentry count: 1=5%, 5=30%, 20=65%
        # This prevents a few re-entries from dominating the score
        reentry_count = r["reentry_count"]
        if reentry_count > 0:
            reentry_score = min(75.0, 5.0 + 25.0 * math.log(reentry_count + 1))
        else:
            reentry_score = 0.0

        # Recency bonus: decays smoothly over 30 days
        recency_bonus = max(0.0, (30 - days_since) * 1.5)  # max 45 at day 0

        attention = min(100.0, reentry_score + recency_bonus)

        # ── Fine-tuned habit score ──
        # Consistency factor: if stddev is low, sessions are at predictable times → high habit
        active_weeks = r["active_weeks"]
        time_stddev = float(r["time_stddev_hours"] or 0)

        if time_stddev > 0 and active_weeks > 0:
            # Lower stddev = more consistent = higher score
            consistency_factor = max(
                0.3, 1.0 - (time_stddev / 168.0)
            )  # 168 = hours in week
        else:
            consistency_factor = 0.5

        habit_base = min(60.0, float(active_weeks) * 12)
        habit = min(100.0, habit_base + 40.0 * consistency_factor)

        # ── Fine-tuned ecosystem score ──
        cross_nav = r["cross_nav_count"]
        if cross_nav > 0:
            # Check diversity: unique destination types
            unique_dest = (
                await pool.fetchval(
                    """SELECT COUNT(DISTINCT meta->>'to_type')
                   FROM behavioral_events
                   WHERE owner_id=$1 AND entity_type=$2 AND entity_id=$3
                     AND event_type='cross_nav'
                     AND occurred_at > now() - INTERVAL '30 days'""",
                    r["owner_id"],
                    r["entity_type"],
                    r["entity_id"],
                )
                or 0
            )
            diversity_bonus = min(1.5, 0.8 + unique_dest * 0.15)
        else:
            diversity_bonus = 1.0

        ecosystem = min(100.0, float(cross_nav) * 20 * diversity_bonus)

        # ── Decay rate (fine-tuned) ──
        lifespan_days = max(
            1.0, (r["last_event"] - r["first_event"]).total_seconds() / 86400
        )
        event_count = r["event_count"]
        if event_count > 0 and lifespan_days > 0:
            # Events per day → higher = lower decay
            events_per_day = event_count / lifespan_days
            decay = min(1.0, max(0.01, 1.0 / (1.0 + events_per_day * 2)))
        else:
            decay = 0.5

        upserts.append(
            (
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                round(attention, 2),
                round(habit, 2),
                round(ecosystem, 2),
                round(decay, 4),
                r["reentry_count"],
            )
        )

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
            owner_id,
            entity_type,
            entity_id,
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
            owner_id,
            keyword,
        )
        await pool.execute(
            "INSERT INTO behavioral_events"
            "(owner_id, entity_type, entity_id, event_type, meta) "
            "VALUES ($1, 'keyword', 0, 'search_repeat', $2)",
            owner_id,
            json.dumps({"keyword": keyword}),
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
        meta = json.dumps(
            {
                "from_type": from_type,
                "from_id": from_id,
                "to_type": to_type,
                "to_id": to_id,
            }
        )
        await pool.execute(
            "INSERT INTO behavioral_events"
            "(owner_id, entity_type, entity_id, event_type, meta) "
            "VALUES ($1, $2, $3, 'cross_nav', $4)",
            owner_id,
            from_type,
            from_id,
            meta,
        )
    except Exception:
        log.exception("record_cross_nav error")


# ── Anomaly detection ────────────────────────────────────────────────────


async def _detect_anomalies(pool: asyncpg.Pool) -> None:
    """
    Detect unusual behavioral patterns and log them as 'anomaly' events.
    Detects (6 types):
    - Decay spike: entity that had high attention_score now has decay_rate > 0.8
    - Affinity dropout: keyword not searched in 14+ days but had affinity > 50
    - Reentry burst: 5+ reentries to same entity in 1 hour (automation signal)
    - Velocity anomaly: события в последний час > 3× от среднего за 7 дней
    - Pattern deviation: attention/ecosystem отклонение > 50% от 7-дневного baseline
    - Schedule deviation: активность в необычное время суток (за пределами нормальных часов)
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
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps(
                    {
                        "type": "decay_spike",
                        "decay_rate": float(r["decay_rate"]),
                        "attention_score": float(r["attention_score"]),
                    }
                ),
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
                json.dumps(
                    {
                        "type": "affinity_dropout",
                        "keyword": r["keyword"],
                        "affinity_score": float(r["affinity_score"]),
                        "days_absent": (
                            datetime.now(tz=timezone.utc) - r["last_searched"]
                        ).days,
                    }
                ),
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
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps({"type": "reentry_burst", "count": int(r["cnt"])}),
            )

        if decay_anomalies or cold_keywords or burst_rows:
            log.info(
                "behavioral_engine anomaly scan: %d decay, %d cold kw, %d bursts",
                len(decay_anomalies),
                len(cold_keywords),
                len(burst_rows),
            )

        # 4. Velocity anomaly — events in last hour vs 7-day hourly average
        velocity_anomalies = await pool.fetch(
            """WITH hourly_now AS (
                   SELECT owner_id, entity_type, entity_id, COUNT(*) AS cnt
                   FROM behavioral_events
                   WHERE occurred_at > now() - INTERVAL '1 hour'
                     AND event_type NOT IN ('anomaly', 'cross_nav')
                   GROUP BY owner_id, entity_type, entity_id
               ), hourly_avg AS (
                   SELECT owner_id, entity_type, entity_id,
                          COUNT(*)::float / GREATEST(1,
                              EXTRACT(EPOCH FROM (now() - MIN(occurred_at))) / 3600
                          ) AS avg_per_hour
                   FROM behavioral_events
                   WHERE occurred_at > now() - INTERVAL '7 days'
                     AND event_type NOT IN ('anomaly', 'cross_nav')
                   GROUP BY owner_id, entity_type, entity_id
               )
               SELECT h.owner_id, h.entity_type, h.entity_id, h.cnt AS current_hour,
                      ROUND(a.avg_per_hour::numeric, 2) AS avg_hourly
               FROM hourly_now h
               JOIN hourly_avg a ON a.owner_id=h.owner_id
                   AND a.entity_type=h.entity_type AND a.entity_id=h.entity_id
               WHERE h.cnt > 10
                 AND h.cnt > a.avg_per_hour * 3
                 AND a.avg_per_hour > 1""",
        )
        for r in velocity_anomalies:
            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, $2, $3, 'anomaly', $4) "
                "ON CONFLICT DO NOTHING",
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps(
                    {
                        "type": "velocity_spike",
                        "current_hour": int(r["current_hour"]),
                        "avg_hourly": float(r["avg_hourly"]),
                        "ratio": round(
                            float(r["current_hour"]) / max(1, float(r["avg_hourly"])), 1
                        ),
                    }
                ),
            )

        # 5. Pattern deviation — current scores deviate >50% from 7-day baseline
        deviation_anomalies = await pool.fetch(
            """WITH baseline AS (
                   SELECT owner_id, entity_type, entity_id,
                          AVG(attention_score) AS avg_att,
                          AVG(ecosystem_score) AS avg_eco,
                          STDDEV(attention_score) AS std_att,
                          STDDEV(ecosystem_score) AS std_eco
                   FROM entity_behavioral_score
                   WHERE updated_at > now() - INTERVAL '7 days'
                     AND updated_at < now() - INTERVAL '3 hours'
                   GROUP BY owner_id, entity_type, entity_id
               ), current AS (
                   SELECT DISTINCT ON (owner_id, entity_type, entity_id)
                          owner_id, entity_type, entity_id,
                          attention_score, ecosystem_score
                   FROM entity_behavioral_score
                   WHERE updated_at > now() - INTERVAL '3 hours'
                   ORDER BY owner_id, entity_type, entity_id, updated_at DESC
               )
               SELECT c.owner_id, c.entity_type, c.entity_id,
                      c.attention_score, c.ecosystem_score,
                      ROUND(b.avg_att::numeric, 2) AS baseline_att,
                      ROUND(b.avg_eco::numeric, 2) AS baseline_eco
               FROM current c
               JOIN baseline b ON b.owner_id=c.owner_id
                   AND b.entity_type=c.entity_type AND b.entity_id=c.entity_id
               WHERE b.avg_att > 10
                 AND (ABS(c.attention_score - b.avg_att) > b.avg_att * 0.5
                      OR ABS(c.ecosystem_score - b.avg_eco) > GREATEST(b.avg_eco * 0.5, 15))""",
        )
        for r in deviation_anomalies:
            dev_type = []
            att_diff = float(r["attention_score"]) - float(r["baseline_att"] or 0)
            eco_diff = float(r["ecosystem_score"]) - float(r["baseline_eco"] or 0)
            if abs(att_diff) > float(r["baseline_att"] or 1) * 0.5:
                dev_type.append("attention_shift")
            if abs(eco_diff) > max(float(r["baseline_eco"] or 0) * 0.5, 15):
                dev_type.append("ecosystem_shift")

            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, $2, $3, 'anomaly', $4) "
                "ON CONFLICT DO NOTHING",
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps(
                    {
                        "type": "pattern_deviation",
                        "subtypes": dev_type,
                        "current_attention": float(r["attention_score"]),
                        "baseline_attention": float(r["baseline_att"] or 0),
                        "current_ecosystem": float(r["ecosystem_score"]),
                        "baseline_ecosystem": float(r["baseline_eco"] or 0),
                    }
                ),
            )

        if velocity_anomalies or deviation_anomalies:
            log.info(
                "behavioral_engine velocity/deviation scan: %d velocity, %d deviations",
                len(velocity_anomalies),
                len(deviation_anomalies),
            )

        # 6. Schedule deviation — activity at unusual hours for this account
        schedule_anomalies = await pool.fetch(
            """WITH hour_dist AS (
                   SELECT owner_id, entity_type, entity_id,
                          EXTRACT(HOUR FROM occurred_at)::int AS hour_of_day,
                          COUNT(*) AS cnt
                   FROM behavioral_events
                   WHERE occurred_at > now() - INTERVAL '30 days'
                     AND event_type NOT IN ('anomaly', 'cross_nav')
                   GROUP BY owner_id, entity_type, entity_id, hour_of_day
               ), top_hours AS (
                   SELECT DISTINCT ON (owner_id, entity_type, entity_id)
                          owner_id, entity_type, entity_id,
                          hour_of_day
                   FROM hour_dist
                   ORDER BY owner_id, entity_type, entity_id, cnt DESC
               ), active_windows AS (
                   SELECT owner_id, entity_type, entity_id,
                          array_agg(hour_of_day ORDER BY cnt DESC) AS active_hours
                   FROM hour_dist
                   WHERE cnt >= 2
                   GROUP BY owner_id, entity_type, entity_id
               ), recent_unusual AS (
                   SELECT e.owner_id, e.entity_type, e.entity_id,
                          EXTRACT(HOUR FROM e.occurred_at)::int AS hour,
                          e.occurred_at
                   FROM behavioral_events e
                   WHERE e.occurred_at > now() - INTERVAL '6 hours'
                     AND e.event_type NOT IN ('anomaly', 'cross_nav')
                     AND EXISTS (
                         SELECT 1 FROM active_windows aw
                         WHERE aw.owner_id=e.owner_id
                           AND aw.entity_type=e.entity_type
                           AND aw.entity_id=e.entity_id
                           AND array_length(aw.active_hours, 1) >= 5
                     )
               )
               SELECT ru.owner_id, ru.entity_type, ru.entity_id,
                      ru.hour AS unusual_hour,
                      aw.active_hours[1:5] AS normal_hours
               FROM recent_unusual ru
               JOIN active_windows aw
                 ON aw.owner_id=ru.owner_id
                 AND aw.entity_type=ru.entity_type
                 AND aw.entity_id=ru.entity_id
               WHERE ru.hour != ALL(aw.active_hours[1:8])
               GROUP BY ru.owner_id, ru.entity_type, ru.entity_id,
                        ru.hour, aw.active_hours
               HAVING COUNT(*) >= 3""",  # at least 3 events at unusual hours
        )
        for r in schedule_anomalies:
            normal = [int(h) for h in (r["normal_hours"] or [])[:5]]
            unusual = int(r["unusual_hour"])
            await pool.execute(
                "INSERT INTO behavioral_events"
                "(owner_id, entity_type, entity_id, event_type, meta) "
                "VALUES ($1, $2, $3, 'anomaly', $4) "
                "ON CONFLICT DO NOTHING",
                r["owner_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps(
                    {
                        "type": "schedule_deviation",
                        "unusual_hour": unusual,
                        "normal_hours": normal,
                        "detail": f"Активность в {unusual}:00, обычно в {normal}",
                    }
                ),
            )

        if schedule_anomalies:
            log.info(
                "behavioral_engine schedule deviation: %d anomalies",
                len(schedule_anomalies),
            )

    except asyncpg.UndefinedTableError:
        log.debug(
            "behavioral_engine anomaly detection skipped (table may not exist yet)"
        )
    except Exception:
        log.exception("behavioral_engine anomaly detection error")


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
        owner_id,
        limit,
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
        owner_id,
        limit,
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
            "SELECT id, bot_id FROM experiments WHERE status='active'"
        )
    except asyncpg.UndefinedTableError:
        log.debug("_auto_conclude_experiments: experiments table not ready yet")
        return
    except Exception:
        log.exception("_auto_conclude_experiments: failed to fetch experiments")
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
                "UPDATE experiments SET status='completed', winner_variant_id=$1 WHERE id=$2",
                winner_id,
                exp["id"],
            )
            concluded += 1
            log.info(
                "behavioral_engine: auto-concluded experiment %d, winner variant %d (z=%.2f)",
                exp["id"],
                winner_id,
                z,
            )
            if bot:
                try:
                    owner = await pool.fetchval(
                        "SELECT added_by FROM managed_bots WHERE bot_id=$1",
                        exp["bot_id"],
                    )
                    if owner:
                        from database import db as _db

                        await _db.notify_if_enabled(
                            pool,
                            bot,
                            owner,
                            "op_complete",
                            f"🧪 <b>A/B эксперимент #{exp['id']} завершён</b>\n\n"
                            f"🏆 Победитель: вариант #{winner_id}\n"
                            f"📊 Z-score: {z:.2f} (95% значимость)\n"
                            f"📈 Показов: {total}",
                        )
                except Exception:
                    log_exc_swallow(
                        log,
                        "Сбой уведомления о завершении A/B-эксперимента",
                        exp_id=exp["id"],
                    )
        except Exception as exc:
            log.debug("auto_conclude exp %d: %s", exp["id"], exc)

    if concluded:
        log.info("behavioral_engine: auto-concluded %d experiments", concluded)
