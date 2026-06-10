"""Background service: compute and maintain account trust scores.

Features:
- Recalculates trust_score every 30 min
- Auto-rotation: автоматически ставит кулдауны low-trust аккаунтам (каждые 6ч)
- Expires cooldowns, decays flood counts
- Writes trust_score history snapshots
"""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from services.account_manager import effective_account_status
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_INTERVAL = 1800  # recalculate every 30 min
_COOLDOWN_HOURS = 2  # cooldown after flood event
_FLOOD_PENALTY = 0.15  # score penalty per flood in last 7 days
_AGE_BONUS_PER_DAY = 0.005
_AGE_BONUS_CAP = 0.30

# Auto-rotation thresholds
_ROTATE_CRITICAL_THRESHOLD = 0.3  # trust < 0.3 → 72h cooldown
_ROTATE_LOW_THRESHOLD = 0.6  # trust 0.3–0.6 → 24h cooldown
_ROTATE_CRITICAL_HOURS = 72
_ROTATE_LOW_HOURS = 24
_ROTATE_INTERVAL_CYCLES = 12  # every 12 cycles (6 hours)


async def _recalculate_scores(pool: asyncpg.Pool) -> None:
    """Recalculate trust_score for all active accounts and persist history snapshot."""
    rows = await pool.fetch("""
        SELECT id, owner_id,
               EXTRACT(EPOCH FROM (NOW() - added_at))/86400 AS age_days,
               flood_count_7d,
               cooldown_until,
               COALESCE(acc_status, 'active') AS acc_status,
               (session_str IS NOT NULL AND session_str <> '') AS has_session
        FROM tg_accounts
        WHERE is_active = true
    """)
    history_batch = []
    for row in rows:
        if effective_account_status(
            row.get("acc_status"),
            has_session=bool(row.get("has_session")),
            is_active=True,
        ) in {"spamblock", "banned", "deactivated", "no_session", "archived"}:
            continue
        age_bonus = min(
            _AGE_BONUS_CAP, float(row["age_days"] or 0) * _AGE_BONUS_PER_DAY
        )
        penalty = _FLOOD_PENALTY * (row["flood_count_7d"] or 0)
        score = max(0.1, min(1.0, 1.0 + age_bonus - penalty))
        # In cooldown → cap score at 0.2
        if row["cooldown_until"]:
            score = min(score, 0.2)
        await pool.execute(
            "UPDATE tg_accounts SET trust_score=$1 WHERE id=$2",
            score,
            row["id"],
        )
        history_batch.append((row["id"], row["owner_id"], score))

    # Корректируем trust для ограниченных аккаунтов, которые могли быть пересчитаны ранее
    try:
        await pool.execute(
            """UPDATE tg_accounts
               SET trust_score = LEAST(COALESCE(trust_score, 1.0), 0.3)
               WHERE is_active = TRUE
                 AND acc_status = 'spamblock'
                 AND COALESCE(trust_score, 1.0) > 0.3"""
        )
        await pool.execute(
            """UPDATE tg_accounts
               SET trust_score = 0.0
               WHERE is_active = TRUE
                 AND acc_status IN ('banned', 'deactivated')
                 AND COALESCE(trust_score, 1.0) > 0.1"""
        )
    except Exception as exc:
        log.debug("trust_engine: restricted account correction skipped: %s", exc)

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
    """Clear cooldown_until for accounts whose cooldown has expired.

    Sets cooldown_cleared_at = NOW() so auto-rotate can apply a grace period
    before re-cooling the same account (preventing perpetual cooldown cycles).
    """
    await pool.execute("""
        UPDATE tg_accounts
        SET cooldown_until = NULL,
            cooldown_cleared_at = NOW()
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
            "WITH d AS (DELETE FROM account_trust_history "
            "WHERE recorded_at < NOW() - INTERVAL '30 days' RETURNING 1) "
            "SELECT COUNT(*) FROM d"
        )
        if deleted:
            log.debug("trust_engine: cleaned %d old history rows", deleted)
    except Exception as exc:
        log.debug("trust_engine history cleanup skipped: %s", exc)


async def _auto_rotate(pool: asyncpg.Pool, bot=None) -> dict:
    """Автоматически ставит кулдауны аккаунтам с низким trust_score.

    Возвращает dict с количеством обработанных аккаунтов для логирования.
    Уведомляет владельцев через bot если передан.
    """
    from database.db import notify_if_enabled

    now = datetime.now(timezone.utc)
    result = {"critical": 0, "low": 0, "notified_owners": set()}

    # Аккаунты с критически низким trust — 72h кулдаун.
    # Grace-period condition: не переназначать кулдаун аккаунтам у которых кулдаун
    # только что истёк (cooldown_cleared_at < 7 дней назад), ЕСЛИ у них нет новых
    # флудов ПОСЛЕ того как кулдаун был снят. Это ломает цикл perpetual cooldown.
    critical_updated = await pool.execute(
        """UPDATE tg_accounts SET cooldown_until = $1
           WHERE is_active = TRUE
             AND trust_score < $2
             AND cooldown_until IS NULL
             AND (
                 -- Никогда не был в кулдауне — применяем
                 cooldown_cleared_at IS NULL
                 -- Флуды появились ПОСЛЕ того как кулдаун был снят — новый инцидент
                 OR (last_flood_at IS NOT NULL AND last_flood_at > cooldown_cleared_at)
                 -- Прошло достаточно времени для восстановления (7 дней = окно декея флудов)
                 OR cooldown_cleared_at < now() - INTERVAL '7 days'
             )""",
        now + timedelta(hours=_ROTATE_CRITICAL_HOURS),
        _ROTATE_CRITICAL_THRESHOLD,
    )
    # Аккаунты с низким trust — 24h кулдаун (те же grace-period условия)
    low_updated = await pool.execute(
        """UPDATE tg_accounts SET cooldown_until = $1
           WHERE is_active = TRUE
             AND trust_score >= $2 AND trust_score < $3
             AND cooldown_until IS NULL
             AND (
                 cooldown_cleared_at IS NULL
                 OR (last_flood_at IS NOT NULL AND last_flood_at > cooldown_cleared_at)
                 OR cooldown_cleared_at < now() - INTERVAL '7 days'
             )""",
        now + timedelta(hours=_ROTATE_LOW_HOURS),
        _ROTATE_CRITICAL_THRESHOLD,
        _ROTATE_LOW_THRESHOLD,
    )

    def _count(pg_result) -> int:
        try:
            return int(str(pg_result).split()[-1])
        except Exception:
            return 0

    crit_n = _count(critical_updated)
    low_n = _count(low_updated)
    result["critical"] = crit_n
    result["low"] = low_n

    if crit_n > 0 or low_n > 0:
        # Получаем per-owner счётчики — показываем каждому ТОЛЬКО его аккаунты
        owner_rows = await pool.fetch(
            """SELECT owner_id,
                      SUM(CASE WHEN trust_score < $1 THEN 1 ELSE 0 END)::int AS owner_crit,
                      SUM(CASE WHEN trust_score >= $1 AND trust_score < $2 THEN 1 ELSE 0 END)::int AS owner_low
               FROM tg_accounts
               WHERE is_active = TRUE
                 AND cooldown_until IS NOT NULL AND cooldown_until > now()
                 AND trust_score < $2
               GROUP BY owner_id""",
            _ROTATE_CRITICAL_THRESHOLD,
            _ROTATE_LOW_THRESHOLD,
        )
        for row in owner_rows:
            owner_id = row["owner_id"]
            owner_crit = row["owner_crit"] or 0
            owner_low = row["owner_low"] or 0
            result["notified_owners"].add(owner_id)
            if bot and (owner_crit > 0 or owner_low > 0):
                try:
                    await notify_if_enabled(
                        pool,
                        bot,
                        owner_id,
                        "flood_warning",
                        "🔄 <b>Авто-ротация аккаунтов</b>\n\n"
                        f"🔴 Критических (trust &lt; {_ROTATE_CRITICAL_THRESHOLD}) → {_ROTATE_CRITICAL_HOURS}ч кулдаун: <b>{owner_crit}</b>\n"
                        f"🟡 Низкий trust ({_ROTATE_CRITICAL_THRESHOLD}–{_ROTATE_LOW_THRESHOLD}) → {_ROTATE_LOW_HOURS}ч кулдаун: <b>{owner_low}</b>\n\n"
                        "Аккаунты не будут использоваться для операций до окончания кулдауна.\n"
                        "Trust score восстановится со временем при отсутствии операций.",
                    )
                except Exception:
                    log_exc_swallow(
                        log, "Сбой notify_if_enabled в auto-rotate", owner_id=owner_id
                    )

        log.info(
            "trust_engine auto-rotate: %d critical (72h), %d low (24h), notified %d owners",
            crit_n,
            low_n,
            len(result["notified_owners"]),
        )

    return result


async def run(pool: asyncpg.Pool, bot=None) -> None:
    """Background loop: recalculate trust scores, auto-rotate, cleanup."""
    await asyncio.sleep(120)  # startup delay
    cycle = 0
    while True:
        try:
            await _release_expired_cooldowns(pool)
            await _decay_flood_counts(pool)
            await _recalculate_scores(pool)
            # Auto-rotate каждые 6 часов (12 циклов × 30min)
            if cycle % _ROTATE_INTERVAL_CYCLES == 0:
                await _auto_rotate(pool, bot)
            # Cleanup old history once per day (48 cycles × 30min = 24h)
            if cycle % 48 == 0:
                await _cleanup_old_history(pool)
            cycle += 1
            log.debug("trust_engine: scores updated (cycle %d)", cycle)
        except Exception as exc:
            log.exception("trust_engine error: %s", exc)
        await asyncio.sleep(_INTERVAL)
