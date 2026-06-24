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


def _bot_token() -> str:
    return os.getenv("BOT_TOKEN", os.getenv("MANAGER_BOT_TOKEN", ""))


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
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query.get("token")
    if not token:
        return None
    return parse_token(token, _bot_token())


async def _safe_count(pool: asyncpg.Pool, query: str, *args) -> int:
    try:
        return int(await pool.fetchval(query, *args) or 0)
    except Exception:
        return 0


async def _safe_fetch(pool: asyncpg.Pool, query: str, *args) -> list:
    try:
        rows = await pool.fetch(query, *args)
        return [dict(r) for r in rows]
    except Exception:
        return []


async def _safe_fetchrow(pool: asyncpg.Pool, query: str, *args) -> dict | None:
    try:
        row = await pool.fetchrow(query, *args)
        return dict(row) if row else None
    except Exception:
        return None


async def _stats(pool: asyncpg.Pool, uid: int) -> dict:
    bots = await _safe_count(pool, "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
    channels = await _safe_count(pool,
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
    subscribers = await _safe_count(pool,
        """SELECT COUNT(DISTINCT bu.user_id)
           FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
           WHERE mb.added_by=$1""", uid)
    campaigns_active = await _safe_count(pool,
        "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1 AND status='running'", uid)
    try:
        funnels_active = int(await pool.fetchval(
            """SELECT COUNT(*) FROM funnel_subscriptions fs
               JOIN funnels f ON f.id=fs.funnel_id
               WHERE f.owner_user_id=$1 AND fs.completed_at IS NULL
                 AND COALESCE(fs.dropped, false)=false""", uid
        ) or 0)
    except Exception:
        try:
            funnels_active = int(await pool.fetchval(
                """SELECT COUNT(*) FROM funnel_subscriptions fs
                   JOIN funnels f ON f.id=fs.funnel_id
                   WHERE f.owner_user_id=$1 AND fs.completed_at IS NULL""", uid
            ) or 0)
        except Exception:
            funnels_active = 0
    return {
        "bots": bots, "channels": channels,
        "subscribers": subscribers, "campaigns_active": campaigns_active,
        "funnels_active": funnels_active,
    }


def setup_routes(app: web.Application, pool: asyncpg.Pool) -> None:

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
        bot_token = _bot_token()
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
        except Exception:
            stats = {"bots": 0, "channels": 0, "subscribers": 0,
                     "campaigns_active": 0, "funnels_active": 0}
        try:
            plan_row = await pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid
            )
            stats["plan"] = (plan_row["current_plan"] if plan_row else "free") or "free"
            stats["plan_expires_at"] = str(plan_row["plan_expires_at"]) if plan_row and plan_row["plan_expires_at"] else None
        except Exception:
            stats["plan"] = "free"
            stats["plan_expires_at"] = None
        try:
            activity = await pool.fetch(
                """SELECT action, status, created_at FROM activity_log
                   WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10""", uid
            )
            stats["recent_activity"] = [dict(r) for r in activity]
        except Exception:
            stats["recent_activity"] = []
        return _json_resp(stats)

    async def bots(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT mb.bot_id, mb.username, mb.first_name, mb.is_active,
                      COUNT(DISTINCT bu.user_id) AS subscriber_count
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id AND bu.is_active=true
               WHERE mb.added_by=$1
               GROUP BY mb.bot_id, mb.username, mb.first_name, mb.is_active
               ORDER BY subscriber_count DESC
               LIMIT 50""", uid)
        return _json_resp({"bots": rows})

    async def bot_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bot_id = request.match_info.get("bot_id")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id", 400)

        bot = await _safe_fetchrow(pool,
            "SELECT bot_id, username, first_name, is_active FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id_int, uid)
        if not bot:
            return _err("Bot not found", 404)

        subs = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)
        total_subs = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id_int)

        recent_broadcasts = await _safe_fetch(pool,
            """SELECT id, message_text, status, sent_count, failed_count, total_users, created_at
               FROM broadcasts WHERE bot_id=$1
               ORDER BY created_at DESC LIMIT 5""", bot_id_int)

        active_funnels = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id_int)
        auto_replies = await _safe_count(pool,
            "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=true", bot_id_int)

        return _json_resp({
            "bot": bot,
            "active_subscribers": subs,
            "total_subscribers": total_subs,
            "recent_broadcasts": recent_broadcasts,
            "active_funnels": active_funnels,
            "auto_replies": auto_replies,
        })

    async def create_broadcast(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")

        bot_id = body.get("bot_id")
        text = (body.get("text") or "").strip()
        if not bot_id or not text:
            return _err("bot_id and text required")
        if len(text) > 4096:
            return _err("Message too long (max 4096 chars)")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id")

        # Verify ownership
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id_int, uid)
        if not owns:
            return _err("Bot not found", 404)

        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)

        try:
            row = await pool.fetchrow(
                """INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by)
                   VALUES($1,$2,$3,'pending',$4)
                   RETURNING id""",
                bot_id_int, text, total, uid
            )
            bcast_id = row["id"]
        except Exception as e:
            log.exception("create_broadcast failed uid=%d bot=%d", uid, bot_id_int)
            return _err("Failed to create broadcast", 500)

        log.info("miniapp: created broadcast id=%d bot=%d uid=%d total=%d", bcast_id, bot_id_int, uid, total)
        return _json_resp({"ok": True, "broadcast_id": bcast_id, "total_users": total})

    async def channels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT channel_id, username, title,
                      COALESCE(members_count, 0) AS member_count,
                      type, added_at
               FROM managed_channels
               WHERE owner_id=$1
               ORDER BY members_count DESC NULLS LAST
               LIMIT 50""", uid)
        return _json_resp({"channels": rows})

    async def campaigns(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, name, status, target_type,
                      sent_count, fail_count AS failed_count,
                      total_targets, created_at
               FROM dm_campaigns
               WHERE owner_id=$1
               ORDER BY created_at DESC
               LIMIT 20""", uid)
        return _json_resp({"campaigns": rows})

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
        except asyncpg.exceptions.UndefinedColumnError:
            try:
                rows = await pool.fetch(
                    """SELECT f.id, f.name, f.is_active,
                              COUNT(fs.id) FILTER (WHERE fs.completed_at IS NULL) AS active_subs,
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
                return _json_resp({"funnels": []})
        except Exception:
            return _json_resp({"funnels": []})

    async def subscription(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                """SELECT s.plan, s.expires_at, s.is_active,
                          p.current_plan
                   FROM platform_users p
                   LEFT JOIN subscriptions s ON s.user_id=p.user_id
                   WHERE p.user_id=$1
                   ORDER BY s.expires_at DESC NULLS LAST
                   LIMIT 1""", uid
            )
            if row:
                return _json_resp({
                    "plan": row["current_plan"] or row["plan"] or "free",
                    "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
                    "is_active": bool(row["is_active"]),
                })
        except Exception:
            pass
        return _json_resp({"plan": "free", "expires_at": None, "is_active": False})

    async def broadcasts_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT b.id, b.bot_id, mb.username AS bot_username,
                      b.message_text, b.status, b.sent_count, b.failed_count,
                      b.total_users, b.created_at
               FROM broadcasts b
               JOIN managed_bots mb ON mb.bot_id=b.bot_id
               WHERE b.created_by=$1
               ORDER BY b.created_at DESC
               LIMIT 20""", uid)
        return _json_resp({"broadcasts": rows})

    # ── SSE real-time stream ────────────────────────────────────────────────

    async def events(request: web.Request) -> web.StreamResponse:
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

        async def fetch_activity() -> list:
            try:
                rows = await pool.fetch(
                    "SELECT action, status, created_at FROM activity_log WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10",
                    uid
                )
                return [dict(r) for r in rows]
            except Exception:
                return []

        try:
            data = await _stats(pool, uid)
            await push("stats", data)
            await push("activity", {"items": await fetch_activity()})

            while True:
                await asyncio.sleep(30)
                data = await _stats(pool, uid)
                await push("stats", data)
                await push("activity", {"items": await fetch_activity()})
                await response.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            log.exception("miniapp/events uid=%d", uid)
        return response

    # ── Route registration ─────────────────────────────────────────────────

    app.router.add_options("/api/miniapp/{path:.*}", handle_options)
    app.router.add_post("/api/miniapp/auth", auth)
    app.router.add_get("/api/miniapp/dashboard", dashboard)
    app.router.add_get("/api/miniapp/bots", bots)
    app.router.add_get("/api/miniapp/bot/{bot_id}", bot_detail)
    app.router.add_post("/api/miniapp/broadcast", create_broadcast)
    app.router.add_get("/api/miniapp/broadcasts", broadcasts_list)
    app.router.add_get("/api/miniapp/channels", channels)
    app.router.add_get("/api/miniapp/campaigns", campaigns)
    app.router.add_get("/api/miniapp/funnels", funnels)
    app.router.add_get("/api/miniapp/subscription", subscription)
    app.router.add_get("/api/miniapp/events", events)

    _static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mini_app")
    _index_path = os.path.join(_static_dir, "index.html")
    if os.path.isdir(_static_dir):
        async def serve_index(request: web.Request) -> web.Response:
            return web.FileResponse(_index_path)
        app.router.add_get("/miniapp", serve_index)
        app.router.add_get("/miniapp/", serve_index)
        app.router.add_static("/miniapp", _static_dir, show_index=False)
        log.info("Mini App static served from %s at /miniapp", _static_dir)
    else:
        log.warning("mini_app/ directory not found — static serving skipped")
