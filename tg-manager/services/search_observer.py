"""Search Observability & Change Detection System.

Pipeline per (keyword × account) observation:

  Telegram search results
    → SNAPSHOT (immutable raw append)
    → OBSERVATION (deterministic fact: found + rank)
    → STATE COMPARISON (last vs current per account)
    → CHANGE EVENT (APPEARED / DISAPPEARED / POSITION_CHANGED)
    → CONFIRMATION WINDOW (second independent observation agrees)
    → ALERT (anti-spam + cooldown enforced)

Invariants:
- No ranking reconstruction or consensus across accounts.
- No smoothing, averaging, or probabilistic inference.
- Every event is deterministic from declared inputs only.
- Materialized state (observation_state) is a cache, never an input source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

# ── Tuning constants ───────────────────────────────────────────────────────

_CONFIRMATION_WINDOW = timedelta(hours=6)
_ALERT_COOLDOWN = timedelta(minutes=30)
_OSCILLATION_WINDOW = timedelta(hours=1)
_SEARCH_LIMIT = 20
_CONFIRMATION_LOOP_INTERVAL = 300  # seconds


# ══════════════════════════════════════════════════════════════════════════
# CANONICALIZATION LAYER
# Pure function — no IO, no external state, no runtime dependencies.
# Input contract: { raw_string: str }
# ══════════════════════════════════════════════════════════════════════════


def canonicalize(raw: str) -> str:
    """Normalize a Telegram username to a stable entity_id.

    Rules (in order): NFC unicode normalize → strip whitespace →
    lowercase → remove leading @ prefix.
    Pure function. Deterministic. No side effects.
    """
    return unicodedata.normalize("NFC", raw).strip().lower().lstrip("@")


# ══════════════════════════════════════════════════════════════════════════
# RAW EVENT LAYER  (append-only, immutable)
# Input contract: { keyword_id, account_id, keyword, results[], truncated }
# ══════════════════════════════════════════════════════════════════════════


async def record_snapshot(
    pool: asyncpg.Pool,
    run_id: str,
    keyword_id: int,
    account_id: int,
    keyword: str,
    results: list[dict[str, Any]],
    truncated: bool = False,
    search_limit: int = _SEARCH_LIMIT,
) -> str:
    """Append one immutable raw snapshot. Returns snapshot_id (UUID string)."""
    snapshot_id = str(uuid.uuid4())
    await pool.execute(
        """INSERT INTO search_snapshots
           (snapshot_id, run_id, keyword_id, account_id, keyword,
            results, result_count, truncated, search_limit)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)""",
        snapshot_id,
        run_id,
        keyword_id,
        account_id,
        keyword,
        json.dumps(results, ensure_ascii=False),
        len(results),
        truncated,
        search_limit,
    )
    return snapshot_id


# ══════════════════════════════════════════════════════════════════════════
# OBSERVATION LAYER  (fact extraction only)
# Input contract: { results[], entity_id }
# Forbidden: IO, external state, probabilistic reasoning.
# ══════════════════════════════════════════════════════════════════════════


def extract_observation(
    results: list[dict[str, Any]],
    entity_id: str,
) -> tuple[bool, int | None]:
    """Deterministic fact extraction from a single results array.

    found = entity_id exists in canonicalized results.
    rank  = first occurrence index + 1; null if not found.
    Only the first occurrence is used; duplicates are ignored.
    Pure function. Only declared inputs. No IO.
    """
    for idx, item in enumerate(results):
        raw = item.get("username") or ""
        if canonicalize(raw) == entity_id:
            # position field is 1-based if set by account_manager;
            # fall back to iteration index (also 1-based) if absent.
            pos = item.get("position")
            return True, int(pos) if pos is not None else idx + 1
    return False, None


async def record_observation(
    pool: asyncpg.Pool,
    snapshot_id: str,
    entity_id: str,
    found: bool,
    rank: int | None,
) -> None:
    """Write a single observation row (idempotent via ON CONFLICT DO NOTHING)."""
    await pool.execute(
        """INSERT INTO search_observations (snapshot_id, entity_id, found, rank)
           VALUES ($1,$2,$3,$4)
           ON CONFLICT (snapshot_id, entity_id) DO NOTHING""",
        snapshot_id,
        entity_id,
        found,
        rank,
    )


# ══════════════════════════════════════════════════════════════════════════
# STATE MODEL  (per keyword × entity × account last-seen cache)
# This is NOT an authoritative data source — only a comparison cache.
# ══════════════════════════════════════════════════════════════════════════


async def _get_last_state(
    pool: asyncpg.Pool,
    keyword_id: int,
    entity_id: str,
    account_id: int,
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """SELECT last_rank, last_found, updated_at
           FROM observation_state
           WHERE keyword_id=$1 AND entity_id=$2 AND account_id=$3""",
        keyword_id,
        entity_id,
        account_id,
    )


async def _upsert_state(
    pool: asyncpg.Pool,
    keyword_id: int,
    entity_id: str,
    account_id: int,
    found: bool,
    rank: int | None,
    snapshot_id: str,
) -> None:
    await pool.execute(
        """INSERT INTO observation_state
           (keyword_id, entity_id, account_id, last_rank, last_found,
            last_snapshot_id, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,now())
           ON CONFLICT (keyword_id, entity_id, account_id) DO UPDATE
           SET last_rank=$4, last_found=$5, last_snapshot_id=$6, updated_at=now()""",
        keyword_id,
        entity_id,
        account_id,
        rank,
        found,
        snapshot_id,
    )


# ══════════════════════════════════════════════════════════════════════════
# CHANGE DETECTION ENGINE  (deterministic state-transition classifier)
# Input contract: { old_found, old_rank, new_found, new_rank }
# Pure function — no IO, no side effects.
# ══════════════════════════════════════════════════════════════════════════


def detect_event_type(
    old_found: bool | None,
    old_rank: int | None,
    new_found: bool,
    new_rank: int | None,
) -> str | None:
    """Return event type or None if no state change / first observation.

    APPEARED       : not found → found
    DISAPPEARED    : found → not found
    POSITION_CHANGED: found → found, rank changed
    None           : no prior state (first observation) or no change
    """
    if old_found is None:
        return None  # first-ever observation, no baseline to compare
    if not old_found and new_found:
        return "APPEARED"
    if old_found and not new_found:
        return "DISAPPEARED"
    if old_found and new_found and old_rank != new_rank:
        return "POSITION_CHANGED"
    return None


async def _record_change_event(
    pool: asyncpg.Pool,
    run_id: str,
    snapshot_id: str,
    keyword_id: int,
    entity_id: str,
    account_id: int,
    event_type: str,
    old_rank: int | None,
    new_rank: int | None,
) -> int:
    """Insert an unconfirmed change event. Returns row id."""
    row_id = await pool.fetchval(
        """INSERT INTO search_change_events
           (run_id, snapshot_id, keyword_id, entity_id, account_id,
            event_type, old_rank, new_rank)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           RETURNING id""",
        run_id,
        snapshot_id,
        keyword_id,
        entity_id,
        account_id,
        event_type,
        old_rank,
        new_rank,
    )
    return row_id


# ══════════════════════════════════════════════════════════════════════════
# FULL PIPELINE  (one call per keyword × account pair)
# ══════════════════════════════════════════════════════════════════════════


async def process_search_result(
    pool: asyncpg.Pool,
    run_id: str,
    keyword_id: int,
    account_id: int,
    keyword: str,
    entity_id: str,
    results: list[dict[str, Any]],
    truncated: bool = False,
) -> str | None:
    """Execute the full observability pipeline for one (keyword × account) pair.

    Steps:
      1. Record immutable raw snapshot
      2. Extract observation fact (found, rank) — pure function
      3. Write observation row
      4. Read prior state (cache only)
      5. Detect state transition — pure function
      6. Record change event if transition detected
      7. Update state cache

    Returns: event_type string if change detected, else None.
    """
    # 1. Raw snapshot (append-only)
    snapshot_id = await record_snapshot(
        pool,
        run_id,
        keyword_id,
        account_id,
        keyword,
        results,
        truncated,
    )

    # 2. Observation extraction (pure)
    found, rank = extract_observation(results, entity_id)

    # 3. Write observation
    await record_observation(pool, snapshot_id, entity_id, found, rank)

    # 4. Prior state (cache read, not authoritative)
    prior = await _get_last_state(pool, keyword_id, entity_id, account_id)
    old_found: bool | None = prior["last_found"] if prior else None
    old_rank: int | None = prior["last_rank"] if prior else None

    # 5. Detect transition (pure)
    event_type = detect_event_type(old_found, old_rank, found, rank)

    # 6. Record change event
    if event_type:
        await _record_change_event(
            pool,
            run_id,
            snapshot_id,
            keyword_id,
            entity_id,
            account_id,
            event_type,
            old_rank,
            rank,
        )
        log.info(
            "search_observer event=%s kw_id=%s entity=%s account=%s old=%s new=%s",
            event_type,
            keyword_id,
            entity_id,
            account_id,
            old_rank,
            rank,
        )

    # 7. Update state cache
    await _upsert_state(
        pool,
        keyword_id,
        entity_id,
        account_id,
        found,
        rank,
        snapshot_id,
    )

    return event_type


# ══════════════════════════════════════════════════════════════════════════
# CONFIRMATION & ALERTING
# ══════════════════════════════════════════════════════════════════════════


async def _is_on_cooldown(
    pool: asyncpg.Pool,
    keyword_id: int,
    entity_id: str,
    event_type: str,
) -> bool:
    row = await pool.fetchrow(
        """SELECT last_alerted FROM search_alert_cooldown
           WHERE keyword_id=$1 AND entity_id=$2 AND event_type=$3""",
        keyword_id,
        entity_id,
        event_type,
    )
    if not row:
        return False
    last = row["last_alerted"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) < _ALERT_COOLDOWN


async def _set_cooldown(
    pool: asyncpg.Pool,
    keyword_id: int,
    entity_id: str,
    event_type: str,
) -> None:
    await pool.execute(
        """INSERT INTO search_alert_cooldown (keyword_id, entity_id, event_type, last_alerted)
           VALUES ($1,$2,$3,now())
           ON CONFLICT (keyword_id, entity_id, event_type) DO UPDATE
           SET last_alerted=now()""",
        keyword_id,
        entity_id,
        event_type,
    )


async def _has_oscillation(
    pool: asyncpg.Pool,
    keyword_id: int,
    entity_id: str,
    event_type: str,
) -> bool:
    """True if the opposite event was confirmed recently (back-and-forth suppression)."""
    opposite = {"APPEARED": "DISAPPEARED", "DISAPPEARED": "APPEARED"}.get(event_type)
    if not opposite:
        return False
    cutoff = datetime.now(timezone.utc) - _OSCILLATION_WINDOW
    row = await pool.fetchrow(
        """SELECT 1 FROM search_change_events
           WHERE keyword_id=$1 AND entity_id=$2 AND event_type=$3
             AND confirmed=TRUE AND occurred_at > $4
           LIMIT 1""",
        keyword_id,
        entity_id,
        opposite,
        cutoff,
    )
    return row is not None


async def _send_alert(pool: asyncpg.Pool, bot: Bot, event: asyncpg.Record) -> None:
    """Send Telegram notification for a confirmed change event via notify_if_enabled."""
    from database import db

    entity = f"@{event['entity_id']}"
    keyword = event["keyword"]
    event_type = event["event_type"]
    owner_id = event["owner_id"]
    old_rank = event["old_rank"]
    new_rank = event["new_rank"]

    if event_type == "APPEARED":
        text = (
            f"🎉 <b>Бот {entity} появился в поиске!</b>\n\n"
            f"🔑 Ключевое слово: «{keyword}»\n"
            f"📍 Позиция: #{new_rank}"
        )
    elif event_type == "DISAPPEARED":
        text = (
            f"⚠️ <b>Бот {entity} исчез из поиска</b>\n\n"
            f"🔑 Ключевое слово: «{keyword}»\n"
            f"📍 Был на позиции: #{old_rank}"
        )
    elif event_type == "POSITION_CHANGED":
        if old_rank and new_rank and new_rank < old_rank:
            icon, direction = "📈", "улучшилась"
        else:
            icon, direction = "📉", "ухудшилась"
        text = (
            f"{icon} <b>Позиция {direction}: {entity}</b>\n\n"
            f"🔑 Ключевое слово: «{keyword}»\n"
            f"📍 #{old_rank} → #{new_rank}"
        )
    else:
        return

    try:
        await db.notify_if_enabled(pool, bot, owner_id, "position_change", text)
        log.info(
            "search_observer alert=%s owner=%s kw=%r entity=%s",
            event_type,
            owner_id,
            keyword,
            event["entity_id"],
        )
    except Exception as exc:
        log.warning("search_observer alert send failed owner=%s: %s", owner_id, exc)


async def _try_confirm_and_alert(
    pool: asyncpg.Pool,
    bot: Bot,
    event: asyncpg.Record,
) -> None:
    """Attempt to confirm a single pending event and send alert if confirmed.

    Confirmation rule:
      A later observation for the same (keyword, entity) from ANY snapshot
      (including same account) agrees with the new state direction.
      For APPEARED / POSITION_CHANGED: found=True in confirming snapshot.
      For DISAPPEARED: found=False in confirming snapshot.

    Anti-spam guards applied before alert:
      1. notify_enabled check
      2. cooldown per (keyword, entity, event_type)
      3. oscillation suppression for APPEARED / DISAPPEARED
    """
    event_type = event["event_type"]
    confirming_found = event_type != "DISAPPEARED"

    confirming = await pool.fetchrow(
        """SELECT so.snapshot_id, so.found, so.rank
           FROM search_observations so
           JOIN search_snapshots ss ON ss.snapshot_id = so.snapshot_id
           WHERE ss.keyword_id = $1
             AND so.entity_id = $2
             AND ss.captured_at > $3
             AND so.found = $4
             AND ss.snapshot_id != $5
           ORDER BY ss.captured_at ASC
           LIMIT 1""",
        event["keyword_id"],
        event["entity_id"],
        event["occurred_at"],
        confirming_found,
        event["snapshot_id"],
    )

    now = datetime.now(timezone.utc)

    if not confirming:
        # Expire events that outlived the confirmation window
        occurred = event["occurred_at"]
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        if (now - occurred) > _CONFIRMATION_WINDOW:
            await pool.execute(
                "UPDATE search_change_events SET confirmed=TRUE, confirmed_at=now() WHERE id=$1",
                event["id"],
            )
        return

    # Mark confirmed
    await pool.execute(
        """UPDATE search_change_events
           SET confirmed=TRUE, confirmed_at=now(), confirming_snapshot_id=$2
           WHERE id=$1""",
        event["id"],
        confirming["snapshot_id"],
    )

    # Guard: notifications disabled for this keyword
    if not event.get("notify_enabled", True):
        return

    # Guard: cooldown
    if await _is_on_cooldown(pool, event["keyword_id"], event["entity_id"], event_type):
        log.debug(
            "search_observer cooldown active for kw_id=%s entity=%s type=%s",
            event["keyword_id"],
            event["entity_id"],
            event_type,
        )
        return

    # Guard: oscillation
    if await _has_oscillation(
        pool, event["keyword_id"], event["entity_id"], event_type
    ):
        log.debug(
            "search_observer oscillation suppressed kw_id=%s entity=%s type=%s",
            event["keyword_id"],
            event["entity_id"],
            event_type,
        )
        return

    # Send alert
    await _send_alert(pool, bot, event)
    await pool.execute(
        "UPDATE search_change_events SET alerted=TRUE, alerted_at=now() WHERE id=$1",
        event["id"],
    )
    await _set_cooldown(pool, event["keyword_id"], event["entity_id"], event_type)


async def run_confirmation_pass(pool: asyncpg.Pool, bot: Bot) -> None:
    """Process all unconfirmed events within the confirmation window."""
    cutoff = datetime.now(timezone.utc) - _CONFIRMATION_WINDOW
    pending = await pool.fetch(
        """SELECT e.id, e.keyword_id, e.entity_id, e.account_id, e.event_type,
                  e.old_rank, e.new_rank, e.occurred_at, e.snapshot_id,
                  tk.keyword, tk.owner_id, tk.bot_id, tk.notify_enabled,
                  mb.username AS bot_username
           FROM search_change_events e
           JOIN tracked_keywords tk ON tk.id = e.keyword_id
           JOIN managed_bots mb ON mb.bot_id = tk.bot_id
           WHERE e.confirmed = FALSE
             AND e.occurred_at > $1
           ORDER BY e.occurred_at ASC""",
        cutoff,
    )
    if not pending:
        return
    log.debug("search_observer confirmation_pass: %d pending events", len(pending))
    for event in pending:
        try:
            await _try_confirm_and_alert(pool, bot, event)
        except Exception as exc:
            log.warning(
                "search_observer confirmation_pass error for event_id=%s: %s",
                event["id"],
                exc,
            )


async def run_confirmation_loop(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background task: run confirmation pass on a fixed interval."""
    await asyncio.sleep(120)  # startup delay
    while True:
        try:
            await run_confirmation_pass(pool, bot)
        except Exception as exc:
            log.exception("search_observer confirmation_loop error: %s", exc)
        await asyncio.sleep(_CONFIRMATION_LOOP_INTERVAL)
