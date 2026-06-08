"""
Flood Intelligence Engine — centralized FloodWait tracking, adaptive pacing,
per-account cooldown management, and operation risk scoring.

Integrates with account_flood_log table (existing) + new flood_intelligence table.
Used by op_worker, account_manager, and any bulk operation handler.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# In-memory per-account flood state (supplements DB for hot-path queries)
_flood_state: dict[int, "_AccountFloodState"] = {}
_ACTION_BASELINES: dict[str, float] = {
    "default": 6.0,
    "join": 55.0,
    "leave": 35.0,
    "invite": 90.0,
    "message": 12.0,
    "mass_publish": 14.0,
}
_ACTION_MIN_TRUST: dict[str, float] = {
    "invite": 0.50,
    "dm": 0.50,
    "dm_campaign": 0.50,
    "message": 0.50,
    "join": 0.35,
    "create_channel": 0.35,
    "create_bot": 0.35,
    "mass_publish": 0.25,
    "post": 0.25,
    "parse": 0.20,
    "default": 0.0,
}


@dataclass
class _AccountFloodState:
    account_id: int
    consecutive_floods: int = 0
    total_floods_24h: int = 0
    last_flood_at: float = 0.0
    cooldown_until: float = 0.0
    risk_score: float = 0.0  # 0.0 = safe, 1.0 = very risky
    action_delays: dict[str, float] = field(
        default_factory=dict
    )  # action_type → delay_s


def get_account_state(account_id: int) -> _AccountFloodState:
    if account_id not in _flood_state:
        _flood_state[account_id] = _AccountFloodState(account_id=account_id)
    return _flood_state[account_id]


def is_account_cooling(account_id: int) -> bool:
    state = get_account_state(account_id)
    return state.cooldown_until > time.monotonic()


def seconds_until_ready(account_id: int) -> float:
    state = get_account_state(account_id)
    remaining = state.cooldown_until - time.monotonic()
    return max(0.0, remaining)


def recommended_delay(account_id: int, action_type: str = "default") -> float:
    """Return recommended delay in seconds before next action for this account."""
    state = get_account_state(account_id)
    baseline = _ACTION_BASELINES.get(action_type, _ACTION_BASELINES["default"])
    learned = state.action_delays.get(
        action_type,
        state.action_delays.get("default", baseline),
    )
    base = max(baseline, learned)
    cooldown_tail = max(0.0, seconds_until_ready(account_id))
    if cooldown_tail > 0:
        base = max(base, min(cooldown_tail + 15.0, cooldown_tail * 1.15))
    multiplier = 1.0 + (state.risk_score * 1.5)
    if state.consecutive_floods >= 2:
        multiplier += min(1.5, state.consecutive_floods * 0.25)
    return min(base * multiplier, 900.0)


def gaussian_delay(
    mean_seconds: float,
    *,
    spread: float = 0.18,
    minimum: float = 0.5,
    maximum: float | None = None,
) -> float:
    """Return a bounded Gaussian delay for conservative load smoothing."""
    stddev = max(mean_seconds * spread, 0.05)
    sampled = random.gauss(mean_seconds, stddev)
    bounded = max(sampled, minimum)
    if maximum is not None:
        bounded = min(bounded, maximum)
    return bounded


def normalize_trust_score(value: object) -> float:
    """Normalize DB trust values to the canonical 0..1 range."""
    try:
        score = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if score > 1.0:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def min_trust_for_action(action_type: str = "default") -> float:
    return _ACTION_MIN_TRUST.get(action_type, _ACTION_MIN_TRUST["default"])


def account_rank_score(account_id: int, trust_score: object) -> float:
    """Lower is better: in-memory risk minus normalized trust weight."""
    return get_account_state(account_id).risk_score - normalize_trust_score(trust_score)


async def record_flood(
    pool: Optional[asyncpg.Pool],
    account_id: int,
    wait_seconds: int,
    action_type: str = "default",
    operation_id: Optional[int] = None,
) -> float:
    """Record a FloodWait event. Returns the actual cooldown seconds applied."""
    state = get_account_state(account_id)
    now = time.monotonic()

    state.consecutive_floods += 1
    state.total_floods_24h += 1
    state.last_flood_at = now

    # Exponential backoff: base wait + consecutive penalty
    penalty = min(state.consecutive_floods * 30, 300)  # up to +5 min penalty
    actual_wait = wait_seconds + penalty
    state.cooldown_until = now + actual_wait

    # Update risk score (increases with floods, decays over time)
    state.risk_score = min(1.0, state.risk_score + 0.2 * state.consecutive_floods)

    # Increase action-specific delay
    current_delay = state.action_delays.get(action_type, 1.0)
    state.action_delays[action_type] = min(current_delay * 1.5, 60.0)  # max 60s delay

    log.warning(
        "FloodWait acc=%d action=%s wait=%ds consecutive=%d cooldown=%.0fs risk=%.2f",
        account_id,
        action_type,
        wait_seconds,
        state.consecutive_floods,
        actual_wait,
        state.risk_score,
    )

    # Persist to DB (non-blocking; skipped when pool is None, e.g. from account_manager)
    if pool is not None:
        try:
            await pool.execute(
                """INSERT INTO account_flood_log(account_id, flood_seconds, action_type)
                   VALUES ($1, $2, $3)""",
                account_id,
                wait_seconds,
                action_type,
            )
            # Update cooldown_until in tg_accounts
            await pool.execute(
                """UPDATE tg_accounts
                   SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'),
                       last_flood_at = NOW(),
                       acc_status = CASE
                           WHEN COALESCE(acc_status, 'active') IN ('spamblock', 'banned', 'deactivated')
                               THEN acc_status
                           ELSE 'cooldown'
                       END,
                       status_reason = $3
                   WHERE id = $2""",
                actual_wait,
                account_id,
                f"{action_type} cooldown after FloodWait ({wait_seconds}s)",
            )
        except Exception as e:
            log.warning("flood_engine DB write failed: %s", e)

    return actual_wait


async def record_success(account_id: int, action_type: str = "default") -> None:
    """Record a successful action — gradually reduce risk score and action delay."""
    state = get_account_state(account_id)
    # Decay risk score on success
    state.risk_score = max(0.0, state.risk_score - 0.05)
    state.consecutive_floods = max(0, state.consecutive_floods - 1)

    # Reduce action delay slightly
    if action_type in state.action_delays:
        state.action_delays[action_type] = max(
            state.action_delays[action_type] * 0.9, 0.5
        )


async def record_peer_flood(
    pool: Optional[asyncpg.Pool],
    account_id: int,
    action_type: str = "default",
    operation_id: Optional[int] = None,
    cooldown_seconds: int = 48 * 3600,
) -> float:
    """Isolate outbound workflows for an account after a peer flood penalty."""
    applied = await record_flood(
        pool=pool,
        account_id=account_id,
        wait_seconds=cooldown_seconds,
        action_type=action_type,
        operation_id=operation_id,
    )
    if pool is not None:
        try:
            await pool.execute(
                """UPDATE tg_accounts
                   SET acc_status = 'spamblock',
                       status_reason = $2
                   WHERE id = $1""",
                account_id,
                f"{action_type} blocked after PeerFlood; outbound workflows paused for 48h",
            )
        except Exception as e:
            log.warning("peer_flood DB write failed: %s", e)
    return applied


async def get_best_account(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    min_trust_score: float | None = None,
) -> dict | None:
    """Select the best available account for an action, considering flood state and risk.

    Optional filters:
      pool_name — restrict to accounts in this pool
      tags      — restrict to accounts having ALL of these tags
    """
    exclude = exclude_ids or []

    conditions = [
        "a.owner_id = $1",
        "a.is_active = TRUE",
        "a.session_str IS NOT NULL",
        "(a.cooldown_until IS NULL OR a.cooldown_until < NOW())",
        "a.id != ALL($2::bigint[])",
    ]
    params: list = [owner_id, exclude]

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool = ${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    min_trust = (
        min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""SELECT a.id, a.session_str, a.first_name, a.phone,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id,
                   a.trust_score, a.cooldown_until, a.tags, a.pool,
                   p.proxy_url, p.geo_country
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST
            LIMIT 10""",
        *params,
    )
    if not rows:
        return None

    # From DB candidates, pick the one with lowest in-memory risk_score
    best = None
    best_score = float("inf")
    for row in rows:
        if is_account_cooling(row["id"]):
            continue
        combined = account_rank_score(row["id"], row["trust_score"])
        if combined < best_score:
            best_score = combined
            best = dict(row)

    return best or (dict(rows[0]) if rows else None)


async def get_active_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    account_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    action_type: str = "default",
    min_trust_score: float | None = None,
) -> list[dict]:
    """Return all active, non-cooling accounts ranked by combined trust/risk score.

    For mass operations that need to cycle through multiple accounts.
    Optional filters: account_ids (restrict to subset), pool_name, tags.
    """
    conditions = [
        "a.owner_id = $1",
        "a.is_active = TRUE",
        "a.session_str IS NOT NULL",
        "(a.cooldown_until IS NULL OR a.cooldown_until < NOW())",
    ]
    params: list = [owner_id]

    if account_ids:
        params.append(account_ids)
        conditions.append(f"a.id = ANY(${len(params)}::bigint[])")

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool = ${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    min_trust = (
        min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""SELECT a.id, a.session_str, a.first_name, a.phone,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id,
                   a.trust_score, a.cooldown_until, a.tags, a.pool,
                   p.proxy_url, p.geo_country
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST""",
        *params,
    )

    # Exclude in-memory cooling accounts, then re-sort by combined score
    result = [dict(r) for r in rows if not is_account_cooling(r["id"])]
    result.sort(key=lambda r: account_rank_score(r["id"], r.get("trust_score")))
    return result


async def wait_if_cooling(account_id: int, action_type: str = "default") -> None:
    """Async wait if account is in cooldown, then apply recommended delay."""
    cool_secs = seconds_until_ready(account_id)
    if cool_secs > 0:
        log.info(
            "flood_engine: acc=%d cooling %.0fs for %s",
            account_id,
            cool_secs,
            action_type,
        )
        await asyncio.sleep(min(cool_secs, 300))  # cap at 5 min wait

    delay = recommended_delay(account_id, action_type)
    if delay > 0.1:
        await asyncio.sleep(
            gaussian_delay(delay, spread=0.12, minimum=0.25, maximum=delay * 1.35)
        )


async def load_state_from_db(pool: asyncpg.Pool, owner_id: int) -> None:
    """Load flood state from DB on startup (for recovery after restart)."""
    rows = await pool.fetch(
        """SELECT a.id, a.trust_score,
                  EXTRACT(EPOCH FROM (a.cooldown_until - NOW())) AS cooldown_remaining,
                  COUNT(fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '24h') AS floods_24h
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id = a.id
           WHERE a.owner_id = $1
           GROUP BY a.id, a.trust_score, a.cooldown_until""",
        owner_id,
    )
    now = time.monotonic()
    for row in rows:
        state = get_account_state(row["id"])
        state.total_floods_24h = row["floods_24h"] or 0
        remaining = row["cooldown_remaining"] or 0
        if remaining > 0:
            state.cooldown_until = now + remaining
        # Estimate risk from 24h flood count
        state.risk_score = min(1.0, (state.total_floods_24h * 0.1))
    log.info(
        "flood_engine: loaded state for %d accounts (owner=%d)", len(rows), owner_id
    )


def get_risk_summary(account_ids: list[int]) -> dict[int, dict]:
    """Return risk summary for a list of accounts."""
    result = {}
    for acc_id in account_ids:
        state = get_account_state(acc_id)
        result[acc_id] = {
            "risk_score": round(state.risk_score, 2),
            "consecutive_floods": state.consecutive_floods,
            "total_floods_24h": state.total_floods_24h,
            "is_cooling": is_account_cooling(acc_id),
            "seconds_until_ready": round(seconds_until_ready(acc_id), 0),
        }
    return result
