"""Physics Engine — operational telemetry + account risk scoring.

Every flood wait and operation completion feeds into risk scoring.
Risk scores are recomputed hourly for all active accounts.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta

import asyncpg

log = logging.getLogger(__name__)

_LOOP_INTERVAL = 3600  # recompute every hour


# ─── Telemetry write (fire-and-forget safe) ───────────────────────────────────


async def record_telemetry(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int | None,
    op_type: str,
    outcome: str,
    flood_wait_s: int = 0,
    duration_ms: int = 0,
) -> None:
    """Record one operation outcome. Never raises."""
    try:
        await pool.execute(
            """INSERT INTO op_telemetry
               (account_id, owner_id, op_type, outcome, flood_wait_s, duration_ms)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            account_id,
            owner_id,
            op_type,
            outcome,
            max(0, flood_wait_s or 0),
            max(0, duration_ms or 0),
        )
    except Exception as e:
        log.debug("physics_engine.record_telemetry: %s", e)


# ─── Risk query ───────────────────────────────────────────────────────────────


async def get_account_risk(pool: asyncpg.Pool, account_id: int) -> dict:
    """Returns risk dict for one account. Never raises."""
    try:
        row = await pool.fetchrow(
            """SELECT risk_score, ban_probability, flood_rate_1h, ops_24h, last_flood_at
               FROM account_risk_scores WHERE account_id=$1""",
            account_id,
        )
        if row:
            return {
                "risk_score": row["risk_score"],
                "ban_probability": row["ban_probability"],
                "flood_rate_1h": row["flood_rate_1h"],
                "ops_24h": row["ops_24h"],
                "last_flood_at": row["last_flood_at"],
            }
    except Exception as e:
        log.debug("physics_engine.get_account_risk: %s", e)
    return {
        "risk_score": 0.0,
        "ban_probability": 0.0,
        "flood_rate_1h": 0.0,
        "ops_24h": 0,
        "last_flood_at": None,
    }


def risk_label(score: float) -> str:
    if score < 0.25:
        return "🟢 Безопасен"
    if score < 0.5:
        return "🟡 Умеренный риск"
    if score < 0.75:
        return "🟠 Высокий риск"
    return "🔴 Критический"


def safe_ops_per_hour(risk_score: float, base: int = 60) -> int:
    """Estimate max safe ops/hour for a given risk score."""
    factor = max(0.05, 1.0 - risk_score)
    return max(1, int(base * factor))


# ─── Score computation ────────────────────────────────────────────────────────


async def _compute_one(pool: asyncpg.Pool, account_id: int) -> None:
    now = datetime.now(timezone.utc)
    rows = await pool.fetch(
        """SELECT outcome, flood_wait_s, created_at
           FROM op_telemetry
           WHERE account_id=$1 AND created_at > NOW() - INTERVAL '7 days'
           ORDER BY created_at DESC
           LIMIT 1000""",
        account_id,
    )
    if not rows:
        return

    total  = len(rows)
    floods = sum(1 for r in rows if r["outcome"] == "flood_wait")
    bans   = sum(1 for r in rows if r["outcome"] == "ban")
    errors = sum(1 for r in rows if r["outcome"] == "error")

    cutoff_1h  = now - timedelta(hours=1)
    cutoff_24h = now - timedelta(hours=24)

    recent_1h = [
        r for r in rows
        if r["created_at"].replace(tzinfo=timezone.utc) > cutoff_1h
    ]
    recent_24h = [
        r for r in rows
        if r["created_at"].replace(tzinfo=timezone.utc) > cutoff_24h
    ]

    floods_1h     = sum(1 for r in recent_1h if r["outcome"] == "flood_wait")
    flood_rate_1h = floods_1h / max(len(recent_1h), 1)
    ops_24h       = len(recent_24h)

    ban_factor    = min(bans * 0.5, 1.0)
    flood_factor  = min(floods / max(total, 1) * 2.0, 1.0)
    recent_factor = min(flood_rate_1h * 3.0, 1.0)

    risk_score = min(
        ban_factor * 0.5 + flood_factor * 0.3 + recent_factor * 0.2,
        1.0,
    )

    # Logistic ban probability — logit clamped to [-50, 50] to prevent exp overflow
    logit    = -3.0 + ban_factor * 5.0 + flood_factor * 2.0 + (errors / max(total, 1)) * 1.5
    logit    = max(-50.0, min(50.0, logit))
    ban_prob = 1.0 / (1.0 + math.exp(-logit))

    last_flood = next(
        (r["created_at"] for r in rows if r["outcome"] == "flood_wait"),
        None,
    )

    await pool.execute(
        """INSERT INTO account_risk_scores
               (account_id, risk_score, ban_probability, flood_rate_1h, ops_24h,
                last_flood_at, computed_at)
           VALUES ($1,$2,$3,$4,$5,$6,NOW())
           ON CONFLICT (account_id) DO UPDATE
               SET risk_score=$2, ban_probability=$3, flood_rate_1h=$4,
                   ops_24h=$5, last_flood_at=$6, computed_at=NOW()""",
        account_id,
        round(risk_score, 4),
        round(ban_prob, 4),
        round(flood_rate_1h, 4),
        ops_24h,
        last_flood,
    )


# ─── Background worker ────────────────────────────────────────────────────────


async def run(pool: asyncpg.Pool, bot) -> None:
    log.info("Physics Engine started")
    while True:
        try:
            accounts = await pool.fetch(
                """SELECT DISTINCT account_id FROM op_telemetry
                   WHERE created_at > NOW() - INTERVAL '7 days'"""
            )
            for acc in accounts:
                try:
                    await _compute_one(pool, acc["account_id"])
                except Exception as e:
                    log.debug(
                        "physics_engine: score error acc=%d: %s",
                        acc["account_id"], e,
                    )
                await asyncio.sleep(0.05)
            if accounts:
                log.info("Physics Engine: scored %d accounts", len(accounts))
        except Exception as e:
            log.error("Physics Engine loop error: %s", e)
        await asyncio.sleep(_LOOP_INTERVAL)
