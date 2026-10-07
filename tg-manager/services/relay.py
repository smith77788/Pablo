"""Hermes Relay: polls managed bots, forwards messages to operator, routes replies back."""

from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api
from config import BOT_TOKEN

log = logging.getLogger(__name__)

# bot_id → last processed update_id
_offsets: dict[int, int] = {}


async def _send_via_management(
    http: aiohttp.ClientSession, operator_id: int, text: str
) -> int | None:
    """Send message to operator via management bot. Returns message_id."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": operator_id, "text": text, "parse_mode": "HTML"}
    try:
        async with http.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
        return data["result"]["message_id"] if data.get("ok") else None
    except Exception:
        log.exception("Failed to forward message to operator %d", operator_id)
        return None


async def _process_bot(
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    bot_id: int,
    token: str,
    operator_id: int,
) -> None:
    try:
        offset = _offsets.get(bot_id, 0)

        if offset == 0:
            # First run — skip old updates
            data = await bot_api._call(
                http, token, "getUpdates", offset=-1, limit=1, timeout=0
            )
            updates = data.get("result", []) if data.get("ok") else []
            _offsets[bot_id] = updates[-1]["update_id"] if updates else 0
            return

        data = await bot_api._call(
            http, token, "getUpdates", offset=offset + 1, limit=100, timeout=0
        )
        updates = data.get("result", []) if data.get("ok") else []
        if not updates:
            return

        bot_row = await pool.fetchrow(
            "SELECT username, first_name FROM managed_bots WHERE bot_id=$1", bot_id
        )
        bot_label = (
            f"@{bot_row['username']}"
            if bot_row and bot_row["username"]
            else (bot_row["first_name"] if bot_row else str(bot_id))
        )

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > _offsets.get(bot_id, 0):
                _offsets[bot_id] = uid

            msg = upd.get("message")
            if not msg:
                continue
            from_user = msg.get("from", {})
            if from_user.get("is_bot"):
                continue

            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text") or msg.get("caption")
            if not chat_id or not text:
                continue

            user_id = from_user.get("id")
            username = from_user.get("username")
            first_name = from_user.get("first_name", "")
            user_label = f"@{username}" if username else first_name

            session_id = await db.get_or_create_relay_session(
                pool, bot_id, user_id, username, first_name
            )

            # Forward to operator with context header
            fwd_text = (
                f"📨 <b>{bot_label}</b>  |  👤 {user_label}\n"
                f"<i>ID: {user_id}</i>\n\n"
                f"{text}\n\n"
                f"<i>← Reply здесь чтобы ответить пользователю</i>"
            )
            fwd_msg_id = await _send_via_management(http, operator_id, fwd_text)
            await db.save_relay_message(pool, session_id, "in", text, fwd_msg_id)

    except Exception:
        log.exception("Relay error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            bots = await db.get_bots_with_relay(pool)
            if bots:
                await asyncio.gather(
                    *(
                        _process_bot(pool, http, b["bot_id"], b["token"], b["added_by"])
                        for b in bots
                    ),
                    return_exceptions=True,
                )
        except Exception:
            log.exception("Relay loop error")
        await asyncio.sleep(30)
