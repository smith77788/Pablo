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
    bots = await _safe_count(pool,
        "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
    channels = await _safe_count(pool,
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
    subscribers = await _safe_count(pool,
        """SELECT COUNT(DISTINCT bu.user_id)
           FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
           WHERE mb.added_by=$1""", uid)
    campaigns_active = await _safe_count(pool,
        "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1 AND status='running'", uid)
    accounts = await _safe_count(pool,
        "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true", uid)
    ops_running = await _safe_count(pool,
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='running'", uid)
    try:
        funnels_active = int(await pool.fetchval(
            """SELECT COUNT(*) FROM funnel_subscriptions fs
               JOIN funnels f ON f.id=fs.funnel_id
               WHERE f.owner_user_id=$1 AND fs.completed_at IS NULL
                 AND COALESCE(fs.dropped, false)=false""", uid) or 0)
    except Exception:
        try:
            funnels_active = int(await pool.fetchval(
                """SELECT COUNT(*) FROM funnel_subscriptions fs
                   JOIN funnels f ON f.id=fs.funnel_id
                   WHERE f.owner_user_id=$1 AND fs.completed_at IS NULL""", uid) or 0)
        except Exception:
            funnels_active = 0
    return {
        "bots": bots,
        "channels": channels,
        "subscribers": subscribers,
        "campaigns_active": campaigns_active,
        "funnels_active": funnels_active,
        "accounts": accounts,
        "ops_running": ops_running,
    }


def setup_routes(app: web.Application, pool: asyncpg.Pool) -> None:

    async def handle_options(request: web.Request) -> web.Response:
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        })

    # ── Auth ────────────────────────────────────────────────────────────────

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

    # ── Dashboard ────────────────────────────────────────────────────────────

    async def dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            stats = await _stats(pool, uid)
        except Exception:
            stats = {"bots": 0, "channels": 0, "subscribers": 0,
                     "campaigns_active": 0, "funnels_active": 0,
                     "accounts": 0, "ops_running": 0}
        try:
            plan_row = await pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid)
            stats["plan"] = (plan_row["current_plan"] if plan_row else "free") or "free"
            stats["plan_expires_at"] = str(plan_row["plan_expires_at"]) if plan_row and plan_row["plan_expires_at"] else None
        except Exception:
            stats["plan"] = "free"
            stats["plan_expires_at"] = None
        try:
            activity = await pool.fetch(
                "SELECT action, status, created_at FROM activity_log WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10",
                uid)
            stats["recent_activity"] = [dict(r) for r in activity]
        except Exception:
            stats["recent_activity"] = []
        return _json_resp(stats)

    # ── Bots ─────────────────────────────────────────────────────────────────

    async def bots(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT mb.bot_id, mb.username, mb.first_name, mb.is_active,
                      COUNT(DISTINCT bu.user_id) FILTER (WHERE bu.is_active=true) AS subscriber_count,
                      COUNT(DISTINCT bu.user_id) AS total_users
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE mb.added_by=$1
               GROUP BY mb.bot_id, mb.username, mb.first_name, mb.is_active
               ORDER BY subscriber_count DESC LIMIT 50""", uid)
        return _json_resp({"bots": rows})

    async def bot_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        bot = await _safe_fetchrow(pool,
            "SELECT bot_id, username, first_name, is_active FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid)
        if not bot:
            return _err("Bot not found", 404)
        subs_active = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id)
        subs_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        funnels_count = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id)
        autoreplies_count = await _safe_count(pool,
            "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=true", bot_id)
        recent_broadcasts = await _safe_fetch(pool,
            """SELECT id, message_text, status, sent_count, failed_count, total_users, created_at
               FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT 5""", bot_id)
        recent_users = await _safe_fetch(pool,
            """SELECT user_id, username, first_name, last_seen, is_active
               FROM bot_users WHERE bot_id=$1 ORDER BY last_seen DESC NULLS LAST LIMIT 10""", bot_id)
        keywords = await _safe_fetch(pool,
            "SELECT keyword, is_active FROM tracked_keywords WHERE bot_id=$1 ORDER BY created_at DESC LIMIT 10",
            bot_id)
        return _json_resp({
            "bot": bot,
            "active_subscribers": subs_active,
            "total_subscribers": subs_total,
            "active_funnels": funnels_count,
            "auto_replies": autoreplies_count,
            "recent_broadcasts": recent_broadcasts,
            "recent_users": recent_users,
            "keywords": keywords,
        })

    async def bot_auto_replies(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        rows = await _safe_fetch(pool,
            "SELECT id, trigger_type, keyword, response_text, is_active, created_at FROM auto_replies WHERE bot_id=$1 ORDER BY created_at DESC",
            bot_id)
        return _json_resp({"auto_replies": rows})

    async def create_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        trigger_type = body.get("trigger_type", "keyword")
        keyword = (body.get("keyword") or "").strip()
        response_text = (body.get("response_text") or "").strip()
        if trigger_type not in ("start", "keyword", "any"):
            return _err("trigger_type must be start/keyword/any")
        if not response_text:
            return _err("response_text required")
        if trigger_type == "keyword" and not keyword:
            return _err("keyword required for keyword trigger")
        try:
            row = await pool.fetchrow(
                "INSERT INTO auto_replies(bot_id, trigger_type, keyword, response_text) VALUES($1,$2,$3,$4) RETURNING id",
                bot_id, trigger_type, keyword or None, response_text)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as e:
            log.exception("create_auto_reply bot=%d uid=%d", bot_id, uid)
            return _err("Failed to create", 500)

    async def toggle_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            reply_id = int(request.match_info["reply_id"])
        except (KeyError, ValueError):
            return _err("Invalid reply_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE auto_replies SET is_active = NOT is_active
                   WHERE id=$1 AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)
                   RETURNING id, is_active""",
                reply_id, uid)
            if not row:
                return _err("Not found", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            return _err("Failed to toggle", 500)

    async def delete_auto_reply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            reply_id = int(request.match_info["reply_id"])
        except (KeyError, ValueError):
            return _err("Invalid reply_id", 400)
        try:
            await pool.execute(
                """DELETE FROM auto_replies WHERE id=$1
                   AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)""",
                reply_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete", 500)

    async def bot_funnels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        rows = await _safe_fetch(pool,
            """SELECT f.id, f.name, f.trigger_type, f.keyword, f.is_active, f.created_at,
                      COUNT(fs.id) AS total_subs,
                      COUNT(fs.id) FILTER (WHERE fs.completed=false) AS active_subs
               FROM funnels f
               LEFT JOIN funnel_subscriptions fs ON fs.funnel_id=f.id
               WHERE f.bot_id=$1
               GROUP BY f.id ORDER BY f.created_at DESC""", bot_id)
        return _json_resp({"funnels": rows})

    async def toggle_funnel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Invalid funnel_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE funnels SET is_active = NOT is_active
                   WHERE id=$1 AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)
                   RETURNING id, is_active""",
                funnel_id, uid)
            if not row:
                return _err("Not found", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            return _err("Failed to toggle", 500)

    # ── Broadcast ────────────────────────────────────────────────────────────

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
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id_int, uid)
        if not owns:
            return _err("Bot not found", 404)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)
        try:
            row = await pool.fetchrow(
                "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) VALUES($1,$2,$3,'pending',$4) RETURNING id",
                bot_id_int, text, total, uid)
            return _json_resp({"ok": True, "broadcast_id": row["id"], "total_users": total})
        except Exception:
            log.exception("create_broadcast bot=%d uid=%d", bot_id_int, uid)
            return _err("Failed to create broadcast", 500)

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
               ORDER BY b.created_at DESC LIMIT 30""", uid)
        return _json_resp({"broadcasts": rows})

    # ── Channels ─────────────────────────────────────────────────────────────

    async def channels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT channel_id, username, title,
                      COALESCE(members_count, 0) AS member_count,
                      type, added_at
               FROM managed_channels WHERE owner_id=$1
               ORDER BY members_count DESC NULLS LAST LIMIT 50""", uid)
        return _json_resp({"channels": rows})

    # ── Campaigns / Funnels ──────────────────────────────────────────────────

    async def campaigns(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, name, status, target_type,
                      sent_count, fail_count AS failed_count,
                      total_targets, created_at
               FROM dm_campaigns WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 20""", uid)
        return _json_resp({"campaigns": rows})

    async def funnels_all(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT f.id, f.name, f.is_active, mb.username AS bot_username,
                          COUNT(fs.id) FILTER (WHERE fs.completed=false) AS active_subs,
                          COUNT(fs.id) FILTER (WHERE fs.completed=true) AS completed_subs
                   FROM funnels f
                   JOIN managed_bots mb ON mb.bot_id=f.bot_id
                   WHERE mb.added_by=$1
                   GROUP BY f.id, f.name, f.is_active, mb.username
                   ORDER BY active_subs DESC LIMIT 30""", uid)
            return _json_resp({"funnels": [dict(r) for r in rows]})
        except Exception:
            return _json_resp({"funnels": []})

    # ── Accounts ─────────────────────────────────────────────────────────────

    async def accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, phone, first_name, username, is_active, last_used, added_at,
                      COALESCE(trust_score, 100) AS trust_score,
                      COALESCE(acc_status, 'ok') AS acc_status,
                      cooldown_until
               FROM tg_accounts WHERE owner_id=$1
               ORDER BY is_active DESC, last_used DESC NULLS LAST LIMIT 100""", uid)
        return _json_resp({"accounts": rows})

    async def account_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Invalid acc_id", 400)
        acc = await _safe_fetchrow(pool,
            """SELECT id, phone, first_name, last_name, username, tg_user_id,
                      is_active, added_at, last_used,
                      COALESCE(trust_score, 100) AS trust_score,
                      COALESCE(acc_status, 'ok') AS acc_status,
                      cooldown_until
               FROM tg_accounts WHERE id=$1 AND owner_id=$2""", acc_id, uid)
        if not acc:
            return _err("Account not found", 404)
        caps = await _safe_fetchrow(pool,
            """SELECT can_invite, can_dm, can_create_channel, can_create_bot,
                      can_set_username, is_premium, has_2fa,
                      daily_dm_limit, daily_invite_limit
               FROM account_capabilities WHERE account_id=$1""", acc_id)
        warmup = await _safe_fetchrow(pool,
            """SELECT current_day, target_days, status, started_at
               FROM account_warmup_plans WHERE account_id=$1
               ORDER BY started_at DESC LIMIT 1""", acc_id)
        recent_ops = await _safe_fetch(pool,
            """SELECT op_type, status, done_items, total_items, created_at
               FROM operation_queue WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 8""", uid)
        return _json_resp({
            "account": acc,
            "capabilities": caps,
            "warmup": warmup,
            "recent_ops": recent_ops,
        })

    async def channel_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Invalid ch_id", 400)
        ch = await _safe_fetchrow(pool,
            """SELECT channel_id, username, title, type,
                      COALESCE(members_count, 0) AS member_count,
                      acc_id, added_at
               FROM managed_channels WHERE channel_id=$1 AND owner_id=$2""", ch_id, uid)
        if not ch:
            return _err("Channel not found", 404)
        acc = None
        if ch and ch.get("acc_id"):
            acc = await _safe_fetchrow(pool,
                "SELECT id, phone, first_name, username FROM tg_accounts WHERE id=$1",
                ch["acc_id"])
        recent_ops = await _safe_fetch(pool,
            """SELECT op_type, status, done_items, total_items, created_at
               FROM operation_queue WHERE owner_id=$1
               ORDER BY created_at DESC LIMIT 8""", uid)
        return _json_resp({
            "channel": ch,
            "linked_account": acc,
            "recent_ops": recent_ops,
        })

    # ── Operations ───────────────────────────────────────────────────────────

    async def operations(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        status_filter = request.query.get("status")
        if status_filter and status_filter not in ("pending", "running", "done", "failed", "cancelled"):
            status_filter = None
        if status_filter:
            rows = await _safe_fetch(pool,
                """SELECT id, op_type, status, total_items, done_items, error_msg, created_at, started_at, finished_at
                   FROM operation_queue WHERE owner_id=$1 AND status=$2
                   ORDER BY created_at DESC LIMIT 30""", uid, status_filter)
        else:
            rows = await _safe_fetch(pool,
                """SELECT id, op_type, status, total_items, done_items, error_msg, created_at, started_at, finished_at
                   FROM operation_queue WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 30""", uid)
        return _json_resp({"operations": rows})

    async def cancel_operation(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Invalid op_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE operation_queue SET status='cancelled'
                   WHERE id=$1 AND owner_id=$2 AND status IN ('pending','running')
                   RETURNING id""",
                op_id, uid)
            if not row:
                return _err("Not found or already finished", 404)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to cancel", 500)

    # ── Analytics ────────────────────────────────────────────────────────────

    async def analytics(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # New users per day (last 7 days) across all user's bots
        growth = await _safe_fetch(pool,
            """SELECT DATE(bu.first_seen) AS day, COUNT(*) AS new_users
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id=bu.bot_id
               WHERE mb.added_by=$1 AND bu.first_seen >= now() - INTERVAL '7 days'
               GROUP BY day ORDER BY day""", uid)
        # Top bots by subscribers
        top_bots = await _safe_fetch(pool,
            """SELECT mb.bot_id, mb.username, mb.first_name,
                      COUNT(DISTINCT bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE mb.added_by=$1
               GROUP BY mb.bot_id, mb.username, mb.first_name
               ORDER BY active_subs DESC LIMIT 5""", uid)
        # Search keywords and last positions
        keywords = await _safe_fetch(pool,
            """SELECT tk.keyword, mb.username AS bot_username,
                      (SELECT sr.position FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_position
               FROM tracked_keywords tk
               JOIN managed_bots mb ON mb.bot_id=tk.bot_id
               WHERE tk.owner_id=$1 AND tk.is_active=true
               ORDER BY tk.created_at DESC LIMIT 10""", uid)
        return _json_resp({
            "growth": growth,
            "top_bots": top_bots,
            "keywords": keywords,
        })

    # ── Subscription ─────────────────────────────────────────────────────────

    async def subscription(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                """SELECT p.current_plan, p.plan_expires_at,
                          s.is_active, s.expires_at AS sub_expires
                   FROM platform_users p
                   LEFT JOIN subscriptions s ON s.user_id=p.user_id AND s.is_active=true
                   WHERE p.user_id=$1
                   ORDER BY s.expires_at DESC NULLS LAST LIMIT 1""", uid)
            if row:
                return _json_resp({
                    "plan": row["current_plan"] or "free",
                    "expires_at": str(row["plan_expires_at"]) if row["plan_expires_at"] else None,
                    "is_active": bool(row["is_active"]),
                })
        except Exception:
            pass
        return _json_resp({"plan": "free", "expires_at": None, "is_active": False})

    async def referral(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        row = await _safe_fetchrow(pool,
            "SELECT code, total_clicks FROM platform_referral_codes WHERE user_id=$1", uid)
        count = await _safe_count(pool,
            "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", uid)
        return _json_resp({
            "code": row["code"] if row else None,
            "total_clicks": row["total_clicks"] if row else 0,
            "total_referrals": count,
        })

    # ── SSE ──────────────────────────────────────────────────────────────────

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
                    uid)
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

    # ── Route registration ────────────────────────────────────────────────────

    app.router.add_options("/api/miniapp/{path:.*}", handle_options)
    app.router.add_post("/api/miniapp/auth", auth)
    app.router.add_get("/api/miniapp/dashboard", dashboard)
    # Bots
    app.router.add_get("/api/miniapp/bots", bots)
    app.router.add_get("/api/miniapp/bot/{bot_id}", bot_detail)
    app.router.add_get("/api/miniapp/bot/{bot_id}/auto_replies", bot_auto_replies)
    app.router.add_post("/api/miniapp/bot/{bot_id}/auto_reply", create_auto_reply)
    app.router.add_put("/api/miniapp/auto_reply/{reply_id}/toggle", toggle_auto_reply)
    app.router.add_delete("/api/miniapp/auto_reply/{reply_id}", delete_auto_reply)
    app.router.add_get("/api/miniapp/bot/{bot_id}/funnels", bot_funnels)
    app.router.add_put("/api/miniapp/funnel/{funnel_id}/toggle", toggle_funnel)
    # Broadcasts
    app.router.add_post("/api/miniapp/broadcast", create_broadcast)
    app.router.add_get("/api/miniapp/broadcasts", broadcasts_list)
    # Channels
    app.router.add_get("/api/miniapp/channels", channels)
    # Campaigns / Funnels
    app.router.add_get("/api/miniapp/campaigns", campaigns)
    app.router.add_get("/api/miniapp/funnels", funnels_all)
    # Accounts
    app.router.add_get("/api/miniapp/accounts", accounts)
    app.router.add_get("/api/miniapp/account/{acc_id}", account_detail)
    # Channels detail
    app.router.add_get("/api/miniapp/channel/{ch_id}", channel_detail)
    # Operations
    app.router.add_get("/api/miniapp/operations", operations)
    app.router.add_post("/api/miniapp/operation/{op_id}/cancel", cancel_operation)
    # Analytics
    app.router.add_get("/api/miniapp/analytics", analytics)
    # Subscription
    app.router.add_get("/api/miniapp/subscription", subscription)
    app.router.add_get("/api/miniapp/referral", referral)
    # SSE
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
