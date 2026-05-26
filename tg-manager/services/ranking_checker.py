"""Background service: search ranking checker with observability pipeline.

Each (keyword × account) pair is an independent observation unit.
Results feed into the Search Observability & Change Detection System
(services/search_observer.py), which handles confirmation + alerting.

search_rankings table is still populated for the UI history display.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import asyncpg
from aiogram import Bot

from database import db
from services.account_manager import search_in_telegram
from services.search_observer import canonicalize, process_search_result

log = logging.getLogger(__name__)

_INTERVAL = 3600         # background sweep every hour
_INTER_SEARCH_DELAY = 5  # seconds between searches (rate limit)


# ── Account selection ──────────────────────────────────────────────────────

async def _get_all_active_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[asyncpg.Record]:
    """Return trusted active accounts ordered by trust_score DESC."""
    from database import db
    return await db.get_trusted_accounts(pool, owner_id)


# ── On-demand check (called from ranking.py handler) ──────────────────────

async def check_bot_keywords(
    pool: asyncpg.Pool,
    bot_id: int,
    owner_id: int,
) -> list[dict[str, Any]]:
    """Check all active keywords for a bot immediately.

    Returns list of { keyword, keyword_id, position, error }.
    Also feeds results into the observability pipeline.
    """
    bot_row = await pool.fetchrow(
        "SELECT username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not bot_row:
        log.warning("check_bot_keywords: bot %s not found for owner %s", bot_id, owner_id)
        return []

    bot_username = (bot_row["username"] or "").lower().lstrip("@")
    entity_id = canonicalize(bot_username)

    accounts = await _get_all_active_accounts(pool, owner_id)
    if not accounts:
        log.warning("check_bot_keywords: no active accounts for owner %s", owner_id)
        return []

    keywords = await pool.fetch(
        "SELECT id, keyword FROM tracked_keywords "
        "WHERE bot_id=$1 AND owner_id=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not keywords:
        return []

    run_id = str(uuid.uuid4())
    results_out: list[dict[str, Any]] = []

    for kw in keywords:
        ui_position: int | None = None
        ui_error = True

        for account in accounts:
            try:
                search_results = await search_in_telegram(
                    account["session_str"], kw["keyword"], _acc=dict(account)
                )

                position: int | None = None
                for r in search_results:
                    if r.get("is_bot") and canonicalize(r.get("username", "")) == entity_id:
                        position = r["position"]
                        break

                # Feed into observability pipeline (each account is independent)
                await process_search_result(
                    pool=pool,
                    run_id=run_id,
                    keyword_id=kw["id"],
                    account_id=account["id"],
                    keyword=kw["keyword"],
                    entity_id=entity_id,
                    results=search_results,
                    truncated=len(search_results) >= 20,
                )

                await db.update_tg_account_used(pool, account["id"])
                log.debug(
                    "check_bot_keywords: kw=%r bot=%r position=%s account=%s",
                    kw["keyword"], bot_username, position, account["id"],
                )

                # Use the first successful account's result for the UI ranking entry
                if ui_error:
                    ui_position = position
                    ui_error = False

                await asyncio.sleep(_INTER_SEARCH_DELAY)

            except Exception as exc:
                from telethon.errors import FloodWaitError
                if isinstance(exc, FloodWaitError):
                    wait = min(exc.seconds + 5, 120)
                    log.warning(
                        "check_bot_keywords FloodWait %ds kw=%r account=%s — sleeping",
                        wait, kw["keyword"], account["id"],
                    )
                    from database import db as _db
                    await _db.record_flood_event(pool, account["id"], operation="ranking_check", flood_seconds=exc.seconds if hasattr(exc, 'seconds') else 0)
                    await asyncio.sleep(wait)
                else:
                    log.warning(
                        "check_bot_keywords: error for %r account=%s: %s",
                        kw["keyword"], account["id"], exc,
                    )

        # Write one UI history entry per keyword sweep
        if not ui_error:
            await pool.execute(
                "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
                kw["id"], bot_id, ui_position,
            )

        results_out.append({
            "keyword": kw["keyword"],
            "keyword_id": kw["id"],
            "position": ui_position,
            "error": ui_error,
        })

    return results_out


# ── Background sweep ───────────────────────────────────────────────────────

async def _check_all(pool: asyncpg.Pool) -> None:
    """Sweep all active keywords across all owners once."""
    keywords = await pool.fetch(
        """SELECT tk.id, tk.keyword, tk.bot_id, tk.owner_id, tk.notify_enabled,
                  mb.username AS bot_username
           FROM tracked_keywords tk
           JOIN managed_bots mb ON mb.bot_id = tk.bot_id
           WHERE tk.is_active = true"""
    )
    if not keywords:
        log.debug("ranking_checker: no active keywords")
        return

    log.info("ranking_checker: sweeping %d keywords", len(keywords))
    run_id = str(uuid.uuid4())

    for kw in keywords:
        accounts = await _get_all_active_accounts(pool, kw["owner_id"])
        if not accounts:
            log.warning(
                "ranking_checker: no active accounts for owner=%s kw=%r — skipping",
                kw["owner_id"], kw["keyword"],
            )
            continue

        bot_username = (kw["bot_username"] or "").lower().lstrip("@")
        entity_id = canonicalize(bot_username)
        ui_position: int | None = None
        ui_written = False

        for account in accounts:
            try:
                search_results = await search_in_telegram(
                    account["session_str"], kw["keyword"], _acc=dict(account)
                )

                position: int | None = None
                for r in search_results:
                    if r.get("is_bot") and canonicalize(r.get("username", "")) == entity_id:
                        position = r["position"]
                        break

                # Observability pipeline — each account is an independent observation
                await process_search_result(
                    pool=pool,
                    run_id=run_id,
                    keyword_id=kw["id"],
                    account_id=account["id"],
                    keyword=kw["keyword"],
                    entity_id=entity_id,
                    results=search_results,
                    truncated=len(search_results) >= 20,
                )

                await db.update_tg_account_used(pool, account["id"])
                log.debug(
                    "ranking_checker: kw=%r bot=%r position=%s account=%s",
                    kw["keyword"], bot_username, position, account["id"],
                )

                # Use first successful account for the UI history entry
                if not ui_written:
                    ui_position = position
                    ui_written = True

                await asyncio.sleep(_INTER_SEARCH_DELAY)

            except Exception as exc:
                from telethon.errors import FloodWaitError
                if isinstance(exc, FloodWaitError):
                    wait = min(exc.seconds + 5, 120)
                    log.warning(
                        "ranking_checker FloodWait %ds kw=%r account=%s — sleeping",
                        wait, kw["keyword"], account["id"],
                    )
                    from database import db as _db
                    await _db.record_flood_event(pool, account["id"], operation="ranking_check", flood_seconds=exc.seconds if hasattr(exc, 'seconds') else 0)
                    await asyncio.sleep(wait)
                else:
                    log.warning(
                        "ranking_checker: error for kw=%r (id=%s) account=%s: %s",
                        kw["keyword"], kw["id"], account["id"], exc,
                    )

        if ui_written:
            await pool.execute(
                "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
                kw["id"], kw["bot_id"], ui_position,
            )


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    """Background loop: sweep all keywords every hour."""
    await asyncio.sleep(60)  # startup delay
    while True:
        try:
            await _check_all(pool)
        except Exception as exc:
            log.exception("ranking_checker._check_all error: %s", exc)
        await asyncio.sleep(_INTERVAL)
