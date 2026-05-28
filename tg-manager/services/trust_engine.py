"""Background service: compute and maintain account trust scores."""
from __future__ import annotations
import asyncio
import logging
import asyncpg

log = logging.getLogger(__name__)

_INTERVAL = 1800          # recalculate every 30 min
_COOLDOWN_HOURS = 2       # cooldown after flood event
_FLOOD_PENALTY = 0.15     # score penalty per flood in last 7 days
_AGE_BONUS_PER_DAY = 0.005
_AGE_BONUS_CAP = 0.30


async def _recalculate_scores(pool: asyncpg.Pool) -> None:
    """Recalculate trust_score for all active accounts and persist history snapshot."""
    rows = await pool.fetch("""
        SELECT id, owner_id,
               EXTRACT(EPOCH FROM (NOW() - added_at))/86400 AS age_days,
               flood_count_7d,
               cooldown_until
        FROM tg_accounts
        WHERE is_active = true
    """)
    history_batch = []
    for row in rows:
        age_bonus = min(_AGE_BONUS_CAP, (row["age_days"] or 0) * _AGE_BONUS_PER_DAY)
        penalty = _FLOOD_PENALTY * (row["flood_count_7d"] or 0)
        score = max(0.1, min(1.0, 1.0 + age_bonus - penalty))
        # In cooldown → cap score at 0.2
        if row["cooldown_until"]:
            score = min(score, 0.2)
        await pool.execute(
            "UPDATE tg_accounts SET trust_score=$1 WHERE id=$2",
            score, row["id"],
        )
        history_batch.append((row["id"], row["owner_id"], score))

    # Write one history snapshot per recalculation cycle (every 30 min)
    if history_batch:
        try:
            await pool.executemany(
                "INSERT INTO account_trust_history(account_id, owner_id, trust_score) "
                "VALUES($1, $2, $3)",
                history_batch,
            )
        except Exception as exc:
            log.debug("trust_history insert skipped: %s", exc)


async def _release_expired_cooldowns(pool: asyncpg.Pool) -> None:
    """Clear cooldown_until for accounts whose cooldown has expired."""
    await pool.execute("""
        UPDATE tg_accounts
        SET cooldown_until = NULL
        WHERE cooldown_until IS NOT NULL AND cooldown_until <= NOW()
    """)


async def _decay_flood_counts(pool: asyncpg.Pool) -> None:
    """Reset flood_count_7d for accounts whose last flood was > 7 days ago."""
    await pool.execute("""
        UPDATE tg_accounts
        SET flood_count_7d = 0
        WHERE flood_count_7d > 0
          AND (last_flood_at IS NULL OR last_flood_at < NOW() - INTERVAL '7 days')
    """)


async def _cleanup_old_history(pool: asyncpg.Pool) -> None:
    """Remove trust score history older than 30 days."""
    try:
        deleted = await pool.fetchval(
            "DELETE FROM account_trust_history "
            "WHERE recorded_at < NOW() - INTERVAL '30 days' "
            "RETURNING COUNT(*)"
        )
        if deleted:
            log.debug("trust_engine: cleaned %d old history rows", deleted)
    except Exception as exc:
        log.debug("trust_engine history cleanup skipped: %s", exc)


async def run(pool: asyncpg.Pool) -> None:
    """Background loop."""
    await asyncio.sleep(120)  # startup delay
    cycle = 0
    while True:
        try:
            await _release_expired_cooldowns(pool)
            await _decay_flood_counts(pool)
            await _recalculate_scores(pool)
            # Cleanup old history once per day (48 cycles × 30min = 24h)
            if cycle % 48 == 0:
                await _cleanup_old_history(pool)
            cycle += 1
            log.debug("trust_engine: scores updated (cycle %d)", cycle)
        except Exception as exc:
            log.exception("trust_engine error: %s", exc)
        await asyncio.sleep(_INTERVAL)
