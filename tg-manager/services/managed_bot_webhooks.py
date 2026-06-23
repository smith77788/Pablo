"""Managed bot webhooks — replace long-polling with HTTP webhooks per bot.

Architecture:
- Each managed bot gets Telegram webhook: POST /tgbot/hook/
- Telegram identifies the bot via X-Telegram-Bot-Api-Secret-Token header
- We store secret -> bot_token in memory (rebuilt on startup from DB)
- Incoming updates go into per-bot asyncio.Queue for auto_responder to consume

Integration:
- Call register_webhook() when a bot is added (bot_factory.py)
- Call unregister_webhook() when a bot is removed
- auto_responder can call get_update_queue(bot_id) to receive updates
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Optional

import asyncpg
import aiohttp

log = logging.getLogger(__name__)

# secret_token (64-char hex) -> bot_token
_secret_map: dict[str, str] = {}

# bot_id -> Queue[dict] of raw Telegram update dicts
_queues: dict[int, asyncio.Queue] = {}


def _make_secret(bot_token: str) -> str:
    """Deterministic per-bot webhook secret (max 256 chars, alphanumeric+_-)."""
    return hashlib.sha256(bot_token.encode()).hexdigest()


def get_update_queue(bot_id: int) -> asyncio.Queue:
    """Return (or create) the asyncio Queue for a managed bot's updates."""
    if bot_id not in _queues:
        _queues[bot_id] = asyncio.Queue(maxsize=2000)
    return _queues[bot_id]


def get_bot_token_by_secret(secret: str) -> Optional[str]:
    """Look up the bot token for an incoming webhook secret."""
    return _secret_map.get(secret)


async def register_webhook(
    http: aiohttp.ClientSession,
    bot_token: str,
    bot_id: int,
    base_url: str,
) -> bool:
    """Register a Telegram webhook for one managed bot.

    base_url: public HTTPS URL of the running server (e.g. https://app.railway.app)
    """
    secret = _make_secret(bot_token)
    _secret_map[secret] = bot_token
    get_update_queue(bot_id)  # ensure queue exists

    webhook_url = f"{base_url.rstrip('/')}/tgbot/hook/"
    tg_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    try:
        async with http.post(tg_url, json={
            "url": webhook_url,
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query", "chat_member"],
            "drop_pending_updates": False,
            "max_connections": 40,
        }) as r:
            data = await r.json()
            if data.get("ok"):
                log.info("managed_bot_webhooks: registered webhook for bot_id=%d", bot_id)
                return True
            log.warning("managed_bot_webhooks: setWebhook failed for bot_id=%d: %s", bot_id, data)
            return False
    except Exception as e:
        log.warning("managed_bot_webhooks: register_webhook error bot_id=%d: %s", bot_id, e)
        return False


async def unregister_webhook(
    http: aiohttp.ClientSession,
    bot_token: str,
    bot_id: int,
) -> bool:
    """Remove the Telegram webhook for a managed bot (back to polling)."""
    secret = _make_secret(bot_token)
    _secret_map.pop(secret, None)

    tg_url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook"
    try:
        async with http.post(tg_url, json={"drop_pending_updates": False}) as r:
            data = await r.json()
            ok = data.get("ok", False)
            log.info("managed_bot_webhooks: unregistered bot_id=%d ok=%s", bot_id, ok)
            return ok
    except Exception as e:
        log.warning("managed_bot_webhooks: unregister error bot_id=%d: %s", bot_id, e)
        return False


async def restore_from_db(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """On startup: re-register webhooks for all active managed bots that have webhooks enabled.

    Only runs if MINI_APP_URL / PUBLIC_BASE_URL is configured.
    """
    base_url = os.getenv("MINI_APP_URL", "") or os.getenv("PUBLIC_BASE_URL", "")
    if not base_url:
        log.debug("managed_bot_webhooks: no base URL configured, skipping webhook restore")
        return

    # Strip /miniapp/ suffix if present
    base_url = base_url.rstrip("/")
    if base_url.endswith("/miniapp"):
        base_url = base_url[:-8]

    try:
        rows = await pool.fetch(
            """SELECT bot_id, token FROM managed_bots
               WHERE is_active = true AND COALESCE(use_webhook, false) = true
               LIMIT 500"""
        )
    except Exception as e:
        log.debug("managed_bot_webhooks: restore_from_db query error: %s", e)
        return

    if not rows:
        return

    log.info("managed_bot_webhooks: restoring webhooks for %d bots", len(rows))
    tasks = [
        register_webhook(http, r["token"], r["bot_id"], base_url)
        for r in rows
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    log.info("managed_bot_webhooks: restored %d/%d webhooks", ok, len(rows))


def make_webhook_route(pool: asyncpg.Pool):
    """Return an aiohttp route handler for POST /tgbot/hook/"""
    import json as _json

    async def handler(request) -> aiohttp.web.Response:
        from aiohttp import web as _web
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        bot_token = get_bot_token_by_secret(secret)
        if not bot_token:
            # Unknown secret — Telegram sent to wrong endpoint or no secret registered
            return _web.Response(status=200, text="OK")  # always 200 to Telegram

        try:
            body = await request.read()
            update = _json.loads(body)
        except Exception:
            return _web.Response(status=200, text="OK")

        # Look up bot_id from token (local cache lookup)
        bot_id = None
        for bid, q in _queues.items():
            # find by matching the secret we stored
            _s = _make_secret(bot_token)
            if _secret_map.get(_s) == bot_token:
                bot_id = bid
                break

        if bot_id is not None:
            q = get_update_queue(bot_id)
            try:
                q.put_nowait({"bot_token": bot_token, "bot_id": bot_id, "update": update})
            except asyncio.QueueFull:
                log.warning("managed_bot_webhooks: queue full for bot_id=%d, update dropped", bot_id)
        else:
            # bot_id unknown — queue with token only, auto_responder can process by token
            log.debug("managed_bot_webhooks: update received for unknown bot_id, token=%s...", bot_token[:10])

        return _web.Response(status=200, text="OK")

    return handler
