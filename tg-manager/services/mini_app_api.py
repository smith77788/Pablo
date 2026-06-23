"""Mini App API — aiohttp routes for the Telegram Mini App + SSE real-time updates."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import asyncpg
from aiohttp import web

from services.mini_app_auth import validate_init_data, make_token, parse_token

log = logging.getLogger(__name__)


def _json_resp(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        status=status,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
        },
    )


def _err(msg: str, status: int = 400) -> web.Response:
    return _json_resp({"error": msg}, status)


def _get_uid(request: web.Request) -> int | None:
    """Extract and validate bearer token, return user_id."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query.get("token")
    if not token:
        return None
    return parse_token(token, os.getenv("MANAGER_BOT_TOKEN", ""))


async def _stats(pool: asyncpg.Pool, uid: int) -> dict:
    bots = int(await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid) or 0)
    channels = int(await pool.fetchval("SELECT COUNT(*) FROM managed_channels WHERE owner_user_id=$1", uid) or 0)
    subscribers = int(await pool.fetchval(
        """SELECT COUNT(DISTINCT bu.user_id)
           FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
           WHERE mb.added_by=$1""", uid
    ) or 0)
    campaigns_active = int(await pool.fetchval(
        "SELECT COUNT(*) FROM dm_campaigns WHERE owner_user_id=$1 AND status='running'", uid
    ) or 0)
    funnels_active = int(await pool.fetchval(
        """SELECT COUNT(*) FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id
           WHERE f.owner_user_id=$1 AND fs.completed_at IS NULL
             AND COALESCE(fs.dropped, false)=false""", uid
    ) or 0)
    return {
        "bots": bots, "channels": channels,
        "subscribers": subscribers, "campaigns_active": campaigns_active,
        "funnels_active": funnels_active,
    }


def setup_routes(app: web.Application, pool: asyncpg.Pool) -> None:
    """Register all Mini App API routes on an existing aiohttp Application."""

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        })

    async def auth(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        init_data = body.get("initData", "")
        bot_token = os.getenv("MANAGER_BOT_TOKEN", "")
        user = validate_init_data(init_data, bot_token)
        if not user:
            return _err("Invalid Telegram initData", 401)
        token = make_token(user["user_id"], bot_token)
        return _json_resp({"token": token, "user": user})

    async def dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            stats = await _stats(pool, uid)
            plan_row = await pool.fetchrow(
                "SELECT current_plan FROM platform_users WHERE user_id=$1", uid
            )
            stats["plan"] = (plan_row["current_plan"] if plan_row else "free") or "free"
            activity = await pool.fetch(
                """SELECT action, status, created_at FROM activity_log
                   WHERE user_id=$1 ORDER BY created_at DESC LIMIT 8""", uid
            )
            stats["recent_activity"] = [dict(r) for r in activity]
            return _json_resp(stats)
        except Exception:
            log.exception("miniapp/dashboard uid=%d", uid)
            return _err("Internal error", 500)

    async def bots(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT mb.bot_id, mb.username, mb.first_name, mb.is_active,
                          COUNT(DISTINCT bu.user_id) AS subscriber_count
                   FROM managed_bots mb
                   LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
                   WHERE mb.added_by=$1
                   GROUP BY mb.bot_id, mb.username, mb.first_name, mb.is_active
                   ORDER BY subscriber_count DESC
                   LIMIT 50""", uid
            )
            return _json_resp({"bots": [dict(r) for r in rows]})
        except Exception:
            log.exception("miniapp/bots uid=%d", uid)
            return _err("Internal error", 500)

    async def channels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT channel_id, username, title, member_count, is_active
                   FROM managed_channels
                   WHERE owner_user_id=$1
                   ORDER BY member_count DESC NULLS LAST
                   LIMIT 50""", uid
            )
            return _json_resp({"channels": [dict(r) for r in rows]})
        except Exception:
            log.exception("miniapp/channels uid=%d", uid)
            return _err("Internal error", 500)

    async def campaigns(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, name, status, target_type, sent_count, failed_count, created_at
                   FROM dm_campaigns
                   WHERE owner_user_id=$1
                   ORDER BY created_at DESC
                   LIMIT 20""", uid
            )
            return _json_resp({"campaigns": [dict(r) for r in rows]})
        except Exception:
            log.exception("miniapp/campaigns uid=%d", uid)
            return _err("Internal error", 500)

    async def funnels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT f.id, f.name, f.is_active,
                          COUNT(fs.id) FILTER (
                              WHERE fs.completed_at IS NULL
                                AND COALESCE(fs.dropped, false) = false
                          ) AS active_subs,
                          COUNT(fs.id) FILTER (WHERE fs.completed_at IS NOT NULL) AS completed_subs
                   FROM funnels f
                   LEFT JOIN funnel_subscriptions fs ON fs.funnel_id=f.id
                   WHERE f.owner_user_id=$1
                   GROUP BY f.id, f.name, f.is_active
                   ORDER BY active_subs DESC
                   LIMIT 20""", uid
            )
            return _json_resp({"funnels": [dict(r) for r in rows]})
        except Exception:
            log.exception("miniapp/funnels uid=%d", uid)
            return _err("Internal error", 500)

    # ── SSE real-time stream ───────────────────────────────────────────────────

    async def events(request: web.Request) -> web.StreamResponse:
        """Server-Sent Events stream — pushes stats every 30s."""
        uid = _get_uid(request)
        if not uid:
            return web.Response(status=401, text="Unauthorized")

        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        })
        await response.prepare(request)

        async def push(event: str, data: Any) -> None:
            payload = json.dumps(data, ensure_ascii=False, default=str)
            await response.write(f"event: {event}\ndata: {payload}\n\n".encode())

        try:
            # Initial push
            data = await _stats(pool, uid)
            await push("stats", data)
            activity = await pool.fetch(
                "SELECT action, status, created_at FROM activity_log WHERE user_id=$1 ORDER BY created_at DESC LIMIT 8", uid
            )
            await push("activity", {"items": [dict(r) for r in activity]})

            # Push every 30 seconds
            while True:
                await asyncio.sleep(30)
                data = await _stats(pool, uid)
                await push("stats", data)
                activity = await pool.fetch(
                    "SELECT action, status, created_at FROM activity_log WHERE user_id=$1 ORDER BY created_at DESC LIMIT 8", uid
                )
                await push("activity", {"items": [dict(r) for r in activity]})
                # SSE comment keepalive (prevents proxy timeouts)
                await response.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            log.exception("miniapp/events uid=%d", uid)
        return response

    # ── Route registration ─────────────────────────────────────────────────────

    app.router.add_options("/api/miniapp/{path:.*}", handle_options)
    app.router.add_post("/api/miniapp/auth", auth)
    app.router.add_get("/api/miniapp/dashboard", dashboard)
    app.router.add_get("/api/miniapp/bots", bots)
    app.router.add_get("/api/miniapp/channels", channels)
    app.router.add_get("/api/miniapp/campaigns", campaigns)
    app.router.add_get("/api/miniapp/funnels", funnels)
    app.router.add_get("/api/miniapp/events", events)

    # Static file serving for Mini App HTML/JS
    _static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mini_app")
    if os.path.isdir(_static_dir):
        app.router.add_static("/miniapp", _static_dir, show_index=True)
        log.info("Mini App static served from %s at /miniapp", _static_dir)
    else:
        log.warning("mini_app/ directory not found — static serving skipped")
