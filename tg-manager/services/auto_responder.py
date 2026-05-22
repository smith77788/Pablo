"""Background auto-reply polling service."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api

log = logging.getLogger(__name__)


def _match_rule(rule: dict, text: str) -> bool:
    if not text:
        return False
    t = rule["trigger_type"]
    if t == "start":
        return text.strip().lower().startswith("/start")
    if t == "keyword":
        return rule["keyword"].lower() in text.lower()
    if t == "any":
        return True
    return False


async def _process_bot(pool: asyncpg.Pool, http: aiohttp.ClientSession,
                       bot_id: int, token: str) -> None:
    try:
        offset = await db.get_update_offset(pool, bot_id)
        data = await bot_api._call(http, token, "getUpdates",
                                   offset=offset + 1 if offset else 0,
                                   limit=100, timeout=0)
        updates = data.get("result", []) if data.get("ok") else []
        if not updates:
            return

        rules = await db.get_active_auto_replies(pool, bot_id)
        max_update_id = offset

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_update_id:
                max_update_id = uid

            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if not chat_id or not text:
                continue

            for rule in rules:
                if _match_rule(rule, text):
                    await bot_api.send_message(http, token, chat_id, rule["response_text"])
                    break  # first matching rule wins

        if max_update_id > offset:
            await db.set_update_offset(pool, bot_id, max_update_id)

    except Exception:
        log.exception("Auto-responder error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            bots = await db.get_bots_with_auto_replies(pool)
            if bots:
                await asyncio.gather(
                    *(_process_bot(pool, http, b["bot_id"], b["token"]) for b in bots),
                    return_exceptions=True,
                )
        except Exception:
            log.exception("Auto-responder loop error")
        await asyncio.sleep(30)
