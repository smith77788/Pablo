"""Background scheduler: fires due scheduled broadcasts every 60 seconds."""

from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import aiohttp
import asyncpg
from database import db
from services import broadcaster
from services import bot_api

log = logging.getLogger(__name__)

# Рассылки опаздывающие больше чем на 1 час → помечаем как 'missed', не запускаем.
# Переопределяется через переменную окружения SCHEDULER_MISSED_THRESHOLD_HOURS.
_MISSED_THRESHOLD = timedelta(
    hours=float(os.environ.get("SCHEDULER_MISSED_THRESHOLD_HOURS", "1"))
)

# Минимальный возраст активного эксперимента до попытки объявить победителя.
# Предотвращает досрочное завершение при малой выборке сразу после старта.
_AB_MIN_AGE = timedelta(hours=float(os.environ.get("AB_WINNER_MIN_AGE_HOURS", "24")))


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    # Track scheduled IDs currently being processed to prevent duplicate firing
    # within a single scheduler cycle (in case processing takes longer than sleep interval).
    _in_flight: set[int] = set()
    _ab_sweep_cycle = 0  # run AB winner sweep every 60 cycles (≈1 hour)

    while True:
        try:
            rows = await db.get_pending_scheduled(pool)
            now = datetime.now(timezone.utc)
            for row in rows:
                # Skip if already being processed in this scheduler instance
                if row["id"] in _in_flight:
                    continue
                execute_at = row["execute_at"]
                # Нормализуем timezone если нужно
                if execute_at is not None and execute_at.tzinfo is None:
                    execute_at = execute_at.replace(tzinfo=timezone.utc)

                # Если рассылка опоздала больше чем на 1 час — помечаем как 'missed'
                if execute_at is not None and (now - execute_at) > _MISSED_THRESHOLD:
                    try:
                        await pool.execute(
                            "UPDATE scheduled_broadcasts SET status='missed' WHERE id=$1",
                            row["id"],
                        )
                        log.warning(
                            "Scheduler: scheduled #%d missed (execute_at=%s, now=%s) — marking missed",
                            row["id"],
                            execute_at,
                            now,
                        )
                    except Exception:
                        log.exception(
                            "Scheduler: failed to mark scheduled #%d as missed",
                            row["id"],
                        )
                    continue

                # Atomically claim this scheduled broadcast to prevent duplicate firing.
                # Uses status='processing' as a transient state; reverted to 'pending'
                # on failure if no broadcast was created yet.
                claimed = await pool.execute(
                    "UPDATE scheduled_broadcasts SET status='processing' "
                    "WHERE id=$1 AND status='pending'",
                    row["id"],
                )
                if claimed == "UPDATE 0":
                    # Already claimed by another scheduler instance or concurrent cycle
                    continue
                _in_flight.add(row["id"])

                bc_id = None
                created_bc = False
                try:
                    # Pre-flight: verify bot token before creating broadcast records
                    me = await bot_api.get_me(http, row["token"])
                    if not me:
                        log.error(
                            "Scheduler: bot token for scheduled #%d (bot_id=%d) is invalid "
                            "or revoked — marking scheduled done to prevent refire",
                            row["id"],
                            row["bot_id"],
                        )
                        try:
                            await pool.execute(
                                "UPDATE scheduled_broadcasts SET status='failed' WHERE id=$1",
                                row["id"],
                            )
                        except Exception:
                            log.exception(
                                "Scheduler: failed to mark scheduled #%d as failed",
                                row["id"],
                            )
                        _in_flight.discard(row["id"])
                        continue

                    total = await db.get_audience_count(pool, row["bot_id"])
                    bc_id = await db.create_broadcast(
                        pool,
                        row["bot_id"],
                        row["message_text"],
                        total,
                        row["created_by"],
                    )
                    created_bc = True
                    broadcaster.start(
                        pool,
                        http,
                        bc_id,
                        row["token"],
                        row["bot_id"],
                        row["message_text"],
                    )
                    await db.mark_scheduled_done(pool, row["id"])
                    _in_flight.discard(row["id"])
                    log.info(
                        "Scheduled #%d fired → broadcast #%d (bot %d)",
                        row["id"],
                        bc_id,
                        row["bot_id"],
                    )
                except Exception:
                    if created_bc and bc_id:
                        log.warning(
                            "Scheduler: broadcast #%d created but start failed for scheduled #%d — "
                            "marking scheduled done to prevent duplicates",
                            bc_id,
                            row["id"],
                        )
                        try:
                            await db.mark_scheduled_done(pool, row["id"])
                        except Exception:
                            log.exception(
                                "Scheduler: failed to mark scheduled #%d as done",
                                row["id"],
                            )
                    else:
                        # No broadcast created — revert claim so it can be retried next cycle
                        try:
                            await pool.execute(
                                "UPDATE scheduled_broadcasts SET status='pending' "
                                "WHERE id=$1 AND status='processing'",
                                row["id"],
                            )
                        except Exception:
                            log.exception(
                                "Scheduler: failed to revert status for scheduled #%d",
                                row["id"],
                            )
                    _in_flight.discard(row["id"])
                    log.exception("Scheduler failed to fire scheduled #%d", row["id"])
        except Exception:
            log.exception("Scheduler loop error")

        # A/B winner sweep — once per hour
        _ab_sweep_cycle += 1
        if _ab_sweep_cycle >= 60:
            _ab_sweep_cycle = 0
            asyncio.get_event_loop().create_task(declare_ab_winners(pool))

        await asyncio.sleep(60)


async def declare_ab_winners(pool: asyncpg.Pool) -> None:
    """Nightly sweep: evaluate all active A/B experiments and declare winners.

    Runs once per hour from the main scheduler loop.  Only evaluates experiments
    that have been active for at least _AB_MIN_AGE to avoid premature decisions.
    """
    try:
        cutoff = datetime.now(timezone.utc) - _AB_MIN_AGE
        active_experiments = await pool.fetch(
            """SELECT id, bot_id, name
               FROM experiments
               WHERE status = 'active'
                 AND COALESCE(started_at, created_at) <= $1""",
            cutoff,
        )
        if not active_experiments:
            return
        log.debug("AB winner sweep: checking %d experiments", len(active_experiments))
        for exp in active_experiments:
            try:
                winner_id = await db.check_experiment_winner(pool, exp["id"])
                if winner_id:
                    log.info(
                        "AB winner declared: experiment %d (bot %d) name=%r winner_variant_id=%d",
                        exp["id"],
                        exp["bot_id"],
                        exp["name"],
                        winner_id,
                    )
            except Exception:
                log.exception(
                    "declare_ab_winners: error evaluating experiment %d", exp["id"]
                )
    except Exception:
        log.exception("declare_ab_winners: sweep failed")
