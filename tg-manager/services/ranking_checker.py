"""Background service: check search ranking for tracked keywords."""
from __future__ import annotations
import asyncio
import logging
import asyncpg
from services.account_manager import search_in_telegram

log = logging.getLogger(__name__)
_INTERVAL = 3600  # check every hour


async def run(pool: asyncpg.Pool) -> None:
    await asyncio.sleep(60)  # startup delay
    while True:
        try:
            await _check_all(pool)
        except Exception as e:
            log.exception("ranking_checker error: %s", e)
        await asyncio.sleep(_INTERVAL)


async def _check_all(pool: asyncpg.Pool) -> None:
    keywords = await pool.fetch(
        "SELECT tk.id, tk.keyword, tk.bot_id, tk.owner_id, mb.username as bot_username "
        "FROM tracked_keywords tk "
        "JOIN managed_bots mb ON mb.bot_id = tk.bot_id "
        "WHERE tk.is_active = true"
    )
    if not keywords:
        return

    for kw in keywords:
        try:
            account = await pool.fetchrow(
                "SELECT session_str FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=true LIMIT 1",
                kw["owner_id"],
            )
            if not account:
                continue

            results = await search_in_telegram(account["session_str"], kw["keyword"])
            bot_username = (kw["bot_username"] or "").lower().lstrip("@")
            position = None
            for r in results:
                if r["is_bot"] and r["username"].lower() == bot_username:
                    position = r["position"]
                    break

            await pool.execute(
                "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
                kw["id"], kw["bot_id"], position,
            )
            await asyncio.sleep(2)  # rate limit between searches
        except Exception as e:
            log.warning("ranking check failed for keyword %s: %s", kw["keyword"], e)
