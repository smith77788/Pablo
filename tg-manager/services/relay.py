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

# Hard cap on concurrent relay sessions to prevent memory leak
_MAX_RELAY_SESSIONS = 200


async def _send_via_management(
    http: aiohttp.ClientSession,
    operator_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> int | None:
    """Send message to operator via management bot. Returns message_id."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": operator_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for attempt in range(3):
        try:
            async with http.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            # Telegram rate-limit: retry_after is in parameters
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if retry_after:
                log.warning(
                    "relay: FloodWait %ds forwarding to operator %d (attempt %d)",
                    retry_after,
                    operator_id,
                    attempt + 1,
                )
                await asyncio.sleep(retry_after + 5)
                continue
            log.warning(
                "relay: sendMessage not ok for operator %d: %s",
                operator_id,
                data.get("description"),
            )
            return None
        except Exception:
            log.exception("Failed to forward message to operator %d", operator_id)
            return None
    return None


async def _process_bot(
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    bot_id: int,
    token: str,
    operator_id: int,
) -> None:
    try:
        # Absence from _offsets means "not yet initialised for this relay session".
        # We must NOT use 0 as "not initialised" because 0 is a valid state
        # (anchor at beginning) and would cause an infinite loop when a bot has
        # no pending updates: every cycle would re-enter the first-run branch.
        initialized = bot_id in _offsets
        offset = _offsets.get(bot_id, 0)

        if not initialized:
            # First run — anchor at the current update_id so we skip messages that
            # arrived before relay was enabled.  If the queue is empty we set
            # offset to 0 (nothing to skip), which is safe: the next call with
            # offset=1 will ask for updates after id=0, i.e. all new messages.
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

        # Guard: don't allow unbounded growth of tracked bots in memory
        if len(_offsets) > _MAX_RELAY_SESSIONS:
            # Evict bots that are no longer in the active set
            active_ids = set(_offsets.keys())
            active_ids.discard(bot_id)
            to_evict = active_ids - {bot_id}
            if to_evict:
                evict_id = next(iter(to_evict))
                _offsets.pop(evict_id, None)
                log.warning(
                    "relay: _offsets hit cap %d — evicted bot_id=%d",
                    _MAX_RELAY_SESSIONS,
                    evict_id,
                )

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
            if not chat_id:
                continue

            user_id = from_user.get("id")
            username = from_user.get("username")
            first_name = from_user.get("first_name", "")
            last_name = from_user.get("last_name", "")
            phone = None

            # Handle contact message — capture phone number
            contact = msg.get("contact")
            if contact and contact.get("user_id") == user_id:
                phone = contact.get("phone_number")

            # Build user label: @username > "Name" > ID
            if username:
                user_label = f"@{username}"
            elif first_name or last_name:
                user_label = f"{first_name} {last_name}".strip()
            else:
                user_label = f"ID:{user_id}"

            text = msg.get("text") or msg.get("caption")
            # Skip non-text, non-contact messages
            if not text and not phone:
                continue

            session_id = await db.get_or_create_relay_session(
                pool, bot_id, user_id, username, first_name
            )

            # Save phone to bot_users if freshly shared
            if phone:
                try:
                    await pool.execute(
                        "UPDATE bot_users SET phone=$1 WHERE bot_id=$2 AND user_id=$3",
                        phone,
                        bot_id,
                        user_id,
                    )
                except Exception:
                    log.exception(
                        "relay: failed to save phone for user %d bot %d",
                        user_id,
                        bot_id,
                    )

            display_text = text or f"📱 Поделился телефоном: {phone}"
            phone_line = f"\n📱 Телефон: <code>{phone}</code>" if phone else ""

            _SUPPORT_TRIGGERS = ("/support", "💬 написать в поддержку")
            is_support_trigger = (text or "").strip().lower() in _SUPPORT_TRIGGERS

            if is_support_trigger:
                # New support request — rich notification with "Open dialog" button
                notify_text = (
                    f"🔔 <b>Новый запрос в поддержку!</b>\n\n"
                    f"🤖 {bot_label}  |  👤 {user_label}\n"
                    f"<i>ID: {user_id}</i>{phone_line}\n\n"
                    f"<i>Нажмите кнопку ниже чтобы открыть диалог и ответить</i>"
                )
                # RelayCb(prefix="rl", action, bot_id, session_id, template_id)
                cb_data = f"rl:session:{bot_id}:{session_id}:0"
                notify_markup = {
                    "inline_keyboard": [[{"text": "💬 Открыть диалог", "callback_data": cb_data}]]
                }
                fwd_msg_id = await _send_via_management(
                    http, operator_id, notify_text, reply_markup=notify_markup
                )
            else:
                # Regular message in existing dialog
                fwd_text = (
                    f"📨 <b>{bot_label}</b>  |  👤 {user_label}\n"
                    f"<i>ID: {user_id}</i>{phone_line}\n\n"
                    f"{display_text}\n\n"
                    f"<i>← Reply здесь чтобы ответить пользователю</i>"
                )
                fwd_msg_id = await _send_via_management(http, operator_id, fwd_text)

            await db.save_relay_message(
                pool, session_id, "in", display_text, fwd_msg_id
            )

    except Exception:
        log.exception("Relay error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    # Relay forwarding is now handled inside auto_responder to avoid getUpdates race condition.
    # This loop only cleans up stale in-memory offset entries.
    while True:
        try:
            bots = await db.get_bots_with_relay(pool)
            active_bot_ids = {b["bot_id"] for b in bots}
            stale = set(_offsets.keys()) - active_bot_ids
            for stale_id in stale:
                _offsets.pop(stale_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Relay cleanup error")
        await asyncio.sleep(300)
