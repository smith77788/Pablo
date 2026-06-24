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
    except Exception as e:
        log.warning("_safe_fetchrow error: %s | query=%.120s", e, query)
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
            """SELECT id, phone, first_name, username, tg_user_id,
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

    async def bot_subscribers(request: web.Request) -> web.Response:
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
        try:
            offset = max(0, int(request.query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name, last_seen, is_active, first_seen
               FROM bot_users WHERE bot_id=$1
               ORDER BY last_seen DESC NULLS LAST
               LIMIT 50 OFFSET $2""", bot_id, offset)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        return _json_resp({"subscribers": rows, "total": total, "offset": offset})

    async def create_dm_campaign(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        name = (body.get("name") or "").strip()
        text_template = (body.get("text_template") or "").strip()
        target_type = body.get("target_type", "all_bots")
        target_id = body.get("target_id")
        if not name:
            return _err("name required")
        if not text_template:
            return _err("text_template required")
        if target_type not in ("bot_users", "all_bots", "crm", "parsed_audience"):
            return _err("Invalid target_type")
        if target_type == "bot_users" and not target_id:
            return _err("target_id required for bot_users target")
        total_targets = 0
        if target_type == "bot_users" and target_id:
            try:
                bot_id_int = int(target_id)
                total_targets = await _safe_count(pool,
                    "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)
            except (TypeError, ValueError):
                pass
        elif target_type == "all_bots":
            total_targets = await _safe_count(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=true""", uid)
        try:
            row = await pool.fetchrow(
                """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, status, total_targets)
                   VALUES($1,$2,$3,$4,$5,'draft',$6) RETURNING id""",
                uid, name, text_template, target_type,
                int(target_id) if target_id else None, total_targets)
            return _json_resp({"ok": True, "id": row["id"], "total_targets": total_targets})
        except Exception:
            log.exception("create_dm_campaign uid=%d", uid)
            return _err("Failed to create campaign", 500)

    async def post_to_channel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        text = (body.get("text") or "").strip()
        if not text:
            return _err("text required")
        if len(text) > 4096:
            return _err("Message too long (max 4096 chars)")
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Channel not found", 404)
        if not ch.get("acc_id"):
            return _err("No linked account for this channel", 400)
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "bulk_post_to_channel", {
                "channel_id": ch_id,
                "account_id": ch["acc_id"],
                "text": text,
            }, total_items=1)
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception:
            log.exception("post_to_channel ch=%d uid=%d", ch_id, uid)
            return _err("Failed to enqueue post", 500)

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

    # ── Deeplinks ────────────────────────────────────────────────────────────

    async def bot_deeplinks(request: web.Request) -> web.Response:
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
        links = await _safe_fetch(pool,
            """SELECT id, name, start_param, click_count, unique_users, created_at
               FROM bot_deep_links WHERE bot_id=$1 ORDER BY click_count DESC LIMIT 30""", bot_id)
        total_clicks = sum(l["click_count"] or 0 for l in links)
        referrals = await _safe_count(pool,
            "SELECT COUNT(*) FROM referrals WHERE bot_id=$1", bot_id)
        return _json_resp({"links": links, "total_clicks": total_clicks, "referrals": referrals})

    async def create_deeplink(request: web.Request) -> web.Response:
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
        name = (body.get("name") or "").strip()
        start_param = (body.get("start_param") or "").strip().lower()
        if not name or not start_param:
            return _err("name and start_param required")
        import re as _re
        if not _re.match(r'^[a-zA-Z0-9_-]{1,64}$', start_param):
            return _err("start_param: only letters, digits, _ and - allowed (max 64 chars)")
        try:
            row = await pool.fetchrow(
                "INSERT INTO bot_deep_links(bot_id, name, start_param) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING id",
                bot_id, name, start_param)
            if not row:
                return _err("start_param already exists for this bot")
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("create_deeplink bot=%d uid=%d", bot_id, uid)
            return _err("Failed to create deeplink", 500)

    async def delete_deeplink(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            link_id = int(request.match_info["link_id"])
        except (KeyError, ValueError):
            return _err("Invalid link_id", 400)
        try:
            await pool.execute(
                """DELETE FROM bot_deep_links WHERE id=$1
                   AND bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$2)""",
                link_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete", 500)

    # ── Engagement segments ───────────────────────────────────────────────────

    async def bot_engagement(request: web.Request) -> web.Response:
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
        # Try user_activity table first, fallback to bot_users.last_seen
        try:
            row = await pool.fetchrow(
                """SELECT
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '1 day') AS hot,
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '7 days'
                                      AND last_seen < now()-INTERVAL '1 day') AS warm,
                     COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '30 days'
                                      AND last_seen < now()-INTERVAL '7 days') AS cold,
                     COUNT(*) FILTER (WHERE last_seen < now()-INTERVAL '30 days') AS lost,
                     COUNT(*) AS total
                   FROM user_activity WHERE bot_id=$1""", bot_id)
        except Exception:
            try:
                row = await pool.fetchrow(
                    """SELECT
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '1 day') AS hot,
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '7 days'
                                          AND last_seen < now()-INTERVAL '1 day') AS warm,
                         COUNT(*) FILTER (WHERE last_seen >= now()-INTERVAL '30 days'
                                          AND last_seen < now()-INTERVAL '7 days') AS cold,
                         COUNT(*) FILTER (WHERE last_seen < now()-INTERVAL '30 days') AS lost,
                         COUNT(*) AS total
                       FROM bot_users WHERE bot_id=$1""", bot_id)
            except Exception:
                row = None
        if not row:
            return _json_resp({"hot": 0, "warm": 0, "cold": 0, "lost": 0, "total": 0})
        return _json_resp({k: int(row[k] or 0) for k in ("hot", "warm", "cold", "lost", "total")})

    # ── Bot Notes ─────────────────────────────────────────────────────────────

    async def bot_note(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        row = await pool.fetchrow(
            "SELECT note FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid,
        )
        if not row:
            return _err("Not found", 404)
        return _json_resp({"note": row["note"] or ""})

    async def save_bot_note(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            body = await request.json()
            note = str(body.get("note", "")).strip()[:2000]
        except Exception:
            return _err("bad body", 400)
        res = await pool.execute(
            "UPDATE managed_bots SET note=$3 WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid, note or None,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

    # ── Bot Commands ───────────────────────────────────────────────────────────

    async def bot_commands(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid,
        )
        if not row:
            return _err("Not found", 404)
        from services import bot_api
        import aiohttp as _ahttp
        async with _ahttp.ClientSession() as sess:
            cmds = await bot_api.get_my_commands(sess, row["token"])
        return _json_resp({"commands": cmds})

    async def set_bot_commands(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            body = await request.json()
            commands = body.get("commands", [])
            if not isinstance(commands, list):
                return _err("commands must be array", 400)
            for c in commands:
                if not isinstance(c, dict) or not c.get("command") or not c.get("description"):
                    return _err("each command must have command and description", 400)
                if len(c["command"]) > 32 or len(c["description"]) > 256:
                    return _err("command or description too long", 400)
        except Exception:
            return _err("bad body", 400)
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid,
        )
        if not row:
            return _err("Not found", 404)
        from services import bot_api
        import aiohttp as _ahttp
        async with _ahttp.ClientSession() as sess:
            if commands:
                ok = await bot_api.set_my_commands(sess, row["token"], commands)
            else:
                ok = await bot_api.delete_my_commands(sess, row["token"])
        if ok:
            return _json_resp({"ok": True, "count": len(commands)})
        return _err("Telegram API error", 500)

    # ── Bot Stats (detailed) ───────────────────────────────────────────────────

    async def bot_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        owned = await pool.fetchval(
            "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
        )
        if not owned:
            return _err("Not found", 404)
        from database import db as _db
        try:
            stats = await _db.get_bot_stats(pool, bot_id)
            daily = await _db.get_audience_daily_growth(pool, bot_id, days=7)
        except Exception as exc:
            log.exception("bot_stats bot=%d uid=%d", bot_id, uid)
            return _err(str(exc), 500)
        daily_list = [{"date": str(r["d"]), "count": int(r["cnt"])} for r in daily]
        return _json_resp({**stats, "daily_growth": daily_list})

    # ── Profile Setter ─────────────────────────────────────────────────────────

    async def profile_setter_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
            uid,
        )
        return _json_resp({"available_accounts": int(total or 0)})

    async def profile_setter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            op = str(body.get("op", "")).strip()
            acc_count = int(body.get("acc_count", 0))
        except Exception:
            return _err("bad body", 400)
        if op not in ("name", "avatar", "2fa"):
            return _err("op must be name|avatar|2fa", 400)
        if acc_count < 0:
            return _err("acc_count must be >= 0", 400)
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
            uid,
        )
        total = int(total or 0)
        use = min(acc_count, total) if acc_count > 0 else total
        if use == 0:
            return _err("Нет доступных аккаунтов", 400)
        rows = await pool.fetch(
            "SELECT id FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
            "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
            uid, use,
        )
        account_ids = [r["id"] for r in rows]
        import json as _json
        params: dict = {"op": op, "account_ids": account_ids}
        if op == "name":
            params["name_data"] = {
                "first_name": str(body.get("first_name", "")).strip()[:64],
                "last_name": str(body.get("last_name", "")).strip()[:64],
                "about": str(body.get("about", "")).strip()[:70],
            }
        elif op == "avatar":
            url = str(body.get("avatar_url", "")).strip()
            if not url.startswith("http"):
                return _err("avatar_url must start with http", 400)
            params["avatar_url"] = url
        elif op == "2fa":
            new_pass = str(body.get("new_password", "")).strip()
            if len(new_pass) < 4:
                return _err("Пароль минимум 4 символа", 400)
            params["new_password"] = new_pass
            params["current_password"] = str(body.get("current_password", "")).strip()
            params["hint"] = str(body.get("hint", "")).strip()
        label_map = {"name": "Имя/Bio", "avatar": "Аватар", "2fa": "2FA пароль"}
        label = f"Сеттер: {label_map.get(op, op)} × {len(account_ids)} акк."
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'bulk_set_profile','pending',$2,$3,$4) RETURNING id",
            uid, _json.dumps(params), len(account_ids), label,
        )
        return _json_resp({"ok": True, "op_id": op_id, "label": label, "count": len(account_ids)})

    # ── Account Cleaner ────────────────────────────────────────────────────────

    async def cleaner_accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await pool.fetch(
            """SELECT id, phone, first_name,
                      (SELECT COUNT(*) FROM managed_channels WHERE acc_id=tg_accounts.id) AS asset_count
               FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL AND session_str <> ''
               ORDER BY added_at""",
            uid,
        )
        return _json_resp({"accounts": [
            {
                "id": r["id"],
                "phone": r["phone"] or "",
                "name": r["first_name"] or r["phone"] or str(r["id"]),
                "asset_count": int(r["asset_count"] or 0),
            }
            for r in rows
        ]})

    async def cleaner_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            op = str(body.get("op", "")).strip()
            account_id = int(body["account_id"])
        except Exception:
            return _err("bad body", 400)
        if op not in ("leave_all_chats", "delete_contacts"):
            return _err("op must be leave_all_chats or delete_contacts", 400)
        row = await pool.fetchrow(
            "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND session_str IS NOT NULL",
            account_id, uid,
        )
        if not row:
            return _err("Аккаунт не найден или нет сессии", 404)
        import json as _json
        label_map = {"leave_all_chats": "Выход из чатов", "delete_contacts": "Удаление контактов"}
        label = f"Cleaner: {label_map[op]} акк #{account_id}"
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,$2,'pending',$3,1,$4) RETURNING id",
            uid, op, _json.dumps({"account_id": account_id}), label,
        )
        return _json_resp({"ok": True, "op_id": op_id, "label": label})

    # ── Relay (Inbox) ─────────────────────────────────────────────────────────

    async def relay_sessions_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        owned = await pool.fetchval(
            "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
        )
        if not owned:
            return _err("Not found", 404)
        try:
            rows = await pool.fetch(
                """SELECT id, user_id, username, first_name, last_activity, messages_count
                   FROM relay_sessions WHERE bot_id=$1 ORDER BY last_activity DESC LIMIT 50""",
                bot_id,
            )
        except Exception as exc:
            log.exception("relay_sessions uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
        return _json_resp({"sessions": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"] or "",
                "name": r["first_name"] or r["username"] or str(r["user_id"]),
                "last_activity": r["last_activity"].isoformat() if r["last_activity"] else None,
                "messages_count": r["messages_count"] or 0,
            }
            for r in rows
        ]})

    async def relay_session_messages(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            session_id = int(request.match_info["session_id"])
        except (KeyError, ValueError):
            return _err("bad session_id", 400)
        # Verify ownership via join
        row = await pool.fetchrow(
            """SELECT rs.id FROM relay_sessions rs
               JOIN managed_bots mb ON mb.bot_id=rs.bot_id
               WHERE rs.id=$1 AND mb.added_by=$2""",
            session_id, uid,
        )
        if not row:
            return _err("Not found", 404)
        msgs = await pool.fetch(
            "SELECT id, direction, text, created_at FROM relay_messages "
            "WHERE session_id=$1 ORDER BY created_at ASC LIMIT 100",
            session_id,
        )
        return _json_resp({"messages": [
            {
                "id": r["id"],
                "direction": r["direction"],
                "text": r["text"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in msgs
        ]})

    async def relay_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
            enabled = bool(body.get("enabled", True))
        except Exception:
            return _err("bad request", 400)
        res = await pool.execute(
            "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2 AND added_by=$3",
            enabled, bot_id, uid,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True, "relay_enabled": enabled})

    # ── API Keys (API Hub) ─────────────────────────────────────────────────────

    async def api_keys_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, name, key_prefix, is_active, created_at, last_used_at
                   FROM api_keys WHERE user_id=$1 ORDER BY created_at DESC""",
                uid,
            )
        except Exception as exc:
            log.exception("api_keys_list uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"keys": [
            {
                "id": r["id"], "name": r["name"] or "",
                "prefix": r["key_prefix"] or "",
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
            }
            for r in rows
        ]})

    async def revoke_api_key(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            key_id = int(request.match_info["key_id"])
        except (KeyError, ValueError):
            return _err("bad key_id", 400)
        res = await pool.execute(
            "UPDATE api_keys SET is_active=FALSE WHERE id=$1 AND user_id=$2",
            key_id, uid,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

    # ── Strike history ─────────────────────────────────────────────────────────

    async def strike_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, op_type, label, status, total_items, processed_items,
                          created_at, finished_at, params
                   FROM operation_queue
                   WHERE owner_id=$1 AND op_type LIKE 'strike%'
                   ORDER BY created_at DESC LIMIT 30""",
                uid,
            )
        except Exception as exc:
            log.exception("strike_history uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"operations": [
            {
                "id": r["id"], "label": r["label"] or r["op_type"],
                "status": r["status"] or "pending",
                "total": r["total_items"] or 0,
                "processed": r["processed_items"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
            }
            for r in rows
        ]})

    # ── Audience Parser (read-only history) ───────────────────────────────────

    async def parser_runs(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, source_type, source_ref, parse_type, status,
                          total_found, total_saved, started_at, finished_at, error
                   FROM parser_runs WHERE owner_id=$1 ORDER BY started_at DESC LIMIT 30""",
                uid,
            )
        except Exception as exc:
            log.exception("parser_runs uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"runs": [
            {
                "id": r["id"],
                "source": r["source_ref"] or "",
                "source_type": r["source_type"] or "",
                "parse_type": r["parse_type"] or "",
                "status": r["status"] or "pending",
                "total_found": r["total_found"] or 0,
                "total_saved": r["total_saved"] or 0,
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "error": (r["error"] or "")[:200],
            }
            for r in rows
        ]})

    async def parsed_audience(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        source = request.rel_url.query.get("source", "")
        limit = min(int(request.rel_url.query.get("limit", "50")), 200)
        offset = int(request.rel_url.query.get("offset", "0"))
        try:
            if source:
                rows = await pool.fetch(
                    """SELECT tg_user_id, username, first_name, last_name, is_premium,
                              source_title, source_type, parsed_at
                       FROM parsed_audiences WHERE owner_id=$1 AND source_username ILIKE $2
                       ORDER BY parsed_at DESC LIMIT $3 OFFSET $4""",
                    uid, f"%{source}%", limit, offset,
                )
            else:
                rows = await pool.fetch(
                    """SELECT tg_user_id, username, first_name, last_name, is_premium,
                              source_title, source_type, parsed_at
                       FROM parsed_audiences WHERE owner_id=$1
                       ORDER BY parsed_at DESC LIMIT $2 OFFSET $3""",
                    uid, limit, offset,
                )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1", uid
            )
        except Exception as exc:
            log.exception("parsed_audience uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({
            "total": int(total or 0),
            "users": [
                {
                    "tg_user_id": r["tg_user_id"],
                    "username": r["username"] or "",
                    "name": " ".join(filter(None, [r["first_name"], r["last_name"]])),
                    "is_premium": bool(r["is_premium"]),
                    "source_title": r["source_title"] or "",
                    "source_type": r["source_type"] or "",
                    "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else None,
                }
                for r in rows
            ],
        })

    async def submit_parse_job(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            source_ref = str(body.get("source_ref", "")).strip().lstrip("@")
            parse_type = str(body.get("parse_type", "members")).strip()
            limit = int(body.get("limit", 500))
        except Exception:
            return _err("bad body", 400)
        if not source_ref:
            return _err("source_ref обязателен", 400)
        if parse_type not in ("members", "active", "comments"):
            parse_type = "members"
        if limit < 1 or limit > 10000:
            limit = 500
        import json as _json
        label = f"Парсинг {parse_type} из @{source_ref} (до {limit})"
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'parse_audience','pending',$2,$3,$4) RETURNING id",
            uid,
            _json.dumps({"source_ref": source_ref, "parse_type": parse_type, "limit": limit}),
            limit, label,
        )
        return _json_resp({"ok": True, "op_id": op_id, "label": label})

    # ── CRM Deals ─────────────────────────────────────────────────────────────

    async def crm_deals(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        stage = request.rel_url.query.get("stage")
        try:
            if stage:
                rows = await pool.fetch(
                    "SELECT id, title, contact, stage, value, notes, created_at, updated_at "
                    "FROM crm_deals WHERE owner_id=$1 AND stage=$2 ORDER BY updated_at DESC LIMIT 50",
                    uid, stage,
                )
            else:
                rows = await pool.fetch(
                    "SELECT id, title, contact, stage, value, notes, created_at, updated_at "
                    "FROM crm_deals WHERE owner_id=$1 ORDER BY updated_at DESC LIMIT 50",
                    uid,
                )
        except Exception as exc:
            log.exception("crm_deals uid=%d", uid)
            return _err(str(exc), 500)
        # Also get pipeline summary
        summary_rows = await pool.fetch(
            "SELECT stage, COUNT(*) AS cnt, COALESCE(SUM(value),0) AS total "
            "FROM crm_deals WHERE owner_id=$1 GROUP BY stage",
            uid,
        )
        summary = {r["stage"]: {"count": int(r["cnt"]), "total": float(r["total"])} for r in summary_rows}
        return _json_resp({
            "deals": [
                {
                    "id": r["id"], "title": r["title"] or "",
                    "contact": r["contact"] or "", "stage": r["stage"] or "lead",
                    "value": float(r["value"]) if r["value"] else 0,
                    "notes": (r["notes"] or "")[:100],
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ],
            "summary": summary,
        })

    async def create_crm_deal(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            title = str(body.get("title", "")).strip()
            contact = str(body.get("contact", "")).strip()
            stage = str(body.get("stage", "lead")).strip()
            value = float(body.get("value", 0) or 0)
            notes = str(body.get("notes", "")).strip()
        except Exception:
            return _err("bad body", 400)
        if not title:
            return _err("title обязателен", 400)
        valid_stages = ("lead", "contact", "proposal", "negotiation", "won", "lost")
        if stage not in valid_stages:
            stage = "lead"
        try:
            deal_id = await pool.fetchval(
                """INSERT INTO crm_deals(owner_id, title, contact, stage, value, notes)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, title, contact or None, stage, value, notes or None,
            )
        except Exception as exc:
            log.exception("create_crm_deal uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "deal_id": deal_id})

    async def update_crm_deal_stage(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            deal_id = int(request.match_info["deal_id"])
            body = await request.json()
            stage = str(body.get("stage", "")).strip()
        except Exception:
            return _err("bad request", 400)
        valid_stages = ("lead", "contact", "proposal", "negotiation", "won", "lost")
        if stage not in valid_stages:
            return _err("invalid stage", 400)
        res = await pool.execute(
            "UPDATE crm_deals SET stage=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
            stage, deal_id, uid,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

    async def delete_crm_deal(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            deal_id = int(request.match_info["deal_id"])
        except (KeyError, ValueError):
            return _err("bad deal_id", 400)
        res = await pool.execute(
            "DELETE FROM crm_deals WHERE id=$1 AND owner_id=$2", deal_id, uid
        )
        if res == "DELETE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

    # ── Workspaces ─────────────────────────────────────────────────────────────

    async def workspaces_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT w.id, w.name, w.description,
                          (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=w.id) AS member_count,
                          wm.role
                   FROM workspaces w
                   JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=$1
                   WHERE w.is_active=TRUE ORDER BY w.created_at DESC""",
                uid,
            )
        except Exception as exc:
            log.exception("workspaces_list uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"workspaces": [
            {
                "id": r["id"], "name": r["name"] or "",
                "description": r["description"] or "",
                "member_count": int(r["member_count"] or 0),
                "role": r["role"] or "member",
            }
            for r in rows
        ]})

    async def create_workspace(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            name = str(body.get("name", "")).strip()[:100]
            description = str(body.get("description", "")).strip()[:500]
        except Exception:
            return _err("bad body", 400)
        if not name:
            return _err("name обязателен", 400)
        try:
            ws_id = await pool.fetchval(
                "INSERT INTO workspaces(owner_id, name, description) VALUES($1,$2,$3) RETURNING id",
                uid, name, description or None,
            )
            await pool.execute(
                "INSERT INTO workspace_members(workspace_id, user_id, role, invited_by) VALUES($1,$2,'owner',$2)",
                ws_id, uid,
            )
        except Exception as exc:
            log.exception("create_workspace uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "ws_id": ws_id})

    async def leave_workspace(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ws_id = int(request.match_info["ws_id"])
        except (KeyError, ValueError):
            return _err("bad ws_id", 400)
        # Check if owner — owners must delete instead of leave
        role = await pool.fetchval(
            "SELECT role FROM workspace_members WHERE workspace_id=$1 AND user_id=$2",
            ws_id, uid,
        )
        if not role:
            return _err("Not a member", 404)
        if role == "owner":
            # Delete workspace entirely
            await pool.execute("DELETE FROM workspaces WHERE id=$1 AND owner_id=$2", ws_id, uid)
        else:
            await pool.execute(
                "DELETE FROM workspace_members WHERE workspace_id=$1 AND user_id=$2", ws_id, uid
            )
        return _json_resp({"ok": True})

    # ── Promo Platform ────────────────────────────────────────────────────────

    async def promo_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            orders = await pool.fetch(
                "SELECT id, keyword, status, target_position, current_subs, target_subs, created_at "
                "FROM promo_orders WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50",
                uid,
            )
            bots = await pool.fetch(
                "SELECT id, bot_username, status, current_subs, ready_at, created_at "
                "FROM bot_warehouse WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50",
                uid,
            )
            panels = await pool.fetch(
                "SELECT id, name, api_url, is_active FROM smm_panels WHERE owner_id=$1 ORDER BY created_at DESC",
                uid,
            )
        except Exception as exc:
            log.exception("promo_overview uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({
            "orders": [
                {
                    "id": r["id"], "keyword": r["keyword"] or "",
                    "status": r["status"] or "waiting",
                    "target_position": r["target_position"],
                    "current_subs": r["current_subs"],
                    "target_subs": r["target_subs"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in orders
            ],
            "bots": [
                {
                    "id": r["id"], "bot_username": r["bot_username"] or "",
                    "status": r["status"] or "aging",
                    "current_subs": r["current_subs"] or 0,
                    "ready_at": r["ready_at"].isoformat() if r["ready_at"] else None,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in bots
            ],
            "panels": [
                {"id": r["id"], "name": r["name"] or "", "api_url": r["api_url"] or "", "is_active": r["is_active"]}
                for r in panels
            ],
        })

    async def promo_cancel_order(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            order_id = int(request.match_info["order_id"])
        except (KeyError, ValueError):
            return _err("bad order_id", 400)
        res = await pool.execute(
            "UPDATE promo_orders SET status='cancelled', updated_at=NOW() WHERE id=$1 AND owner_id=$2",
            order_id, uid,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

    async def promo_create_order_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            keyword = str(body.get("keyword", "")).strip()
            target_position = int(body.get("target_position", 1))
            target_subs = body.get("target_subs")
            bot_id = body.get("bot_id")
            smm_panel_id = body.get("smm_panel_id")
        except Exception:
            return _err("bad body", 400)
        if not keyword:
            return _err("keyword обязателен", 400)
        if target_position < 1 or target_position > 50:
            return _err("target_position 1-50", 400)
        try:
            order_id = await pool.fetchval(
                """INSERT INTO promo_orders(owner_id, keyword, target_position, bot_id, smm_panel_id, target_subs)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, keyword, target_position,
                int(bot_id) if bot_id else None,
                int(smm_panel_id) if smm_panel_id else None,
                int(target_subs) if target_subs else None,
            )
        except Exception as exc:
            log.exception("promo_create_order uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "order_id": order_id})

    async def promo_add_warehouse_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            bot_username = str(body.get("bot_username", "")).strip().lstrip("@")
        except Exception:
            return _err("bad body", 400)
        if not bot_username:
            return _err("bot_username обязателен", 400)
        try:
            from datetime import datetime, timezone, timedelta
            now = datetime.now(tz=timezone.utc)
            ready_at = now + timedelta(days=21)
            bot_id = await pool.fetchval(
                """INSERT INTO bot_warehouse(owner_id, bot_username, status, registered_at, ready_at)
                   VALUES($1,$2,'aging',$3,$4) RETURNING id""",
                uid, bot_username, now, ready_at,
            )
        except Exception as exc:
            log.exception("promo_add_warehouse_bot uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "bot_id": bot_id, "ready_at": ready_at.isoformat()})

    # ── Error Reports ─────────────────────────────────────────────────────────

    async def submit_error_report(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            description = str(body.get("description", "")).strip()
            context = body.get("context", None)
        except Exception:
            return _err("bad body", 400)
        if len(description) < 10:
            return _err("Описание слишком короткое (мин. 10 символов)", 400)
        if len(description) > 2000:
            return _err("Описание слишком длинное (макс. 2000 символов)", 400)
        try:
            report_id = await pool.fetchval(
                """INSERT INTO error_reports(user_id, description, context, status)
                   VALUES($1,$2,$3,'new') RETURNING id""",
                uid, description,
                __import__("json").dumps(context) if context else None,
            )
        except Exception as exc:
            log.exception("submit_error_report uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "report_id": report_id})

    async def my_error_reports(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await pool.fetch(
            "SELECT id, description, status, created_at FROM error_reports "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
            uid,
        )
        return _json_resp({"reports": [
            {
                "id": r["id"],
                "description": (r["description"] or "")[:120],
                "status": r["status"] or "new",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]})

    # ── Bot toggle / edit ─────────────────────────────────────────────────────

    async def toggle_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE managed_bots SET is_active = NOT is_active
                   WHERE bot_id=$1 AND added_by=$2 RETURNING bot_id, is_active""",
                bot_id, uid)
            if not row:
                return _err("Bot not found", 404)
            return _json_resp({"ok": True, "is_active": row["is_active"]})
        except Exception:
            return _err("Failed to toggle bot", 500)

    # ── Funnel Steps ──────────────────────────────────────────────────────────

    async def funnel_steps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("Invalid funnel_id", 400)
        # Verify ownership via managed_bots
        funnel = await _safe_fetchrow(pool,
            """SELECT f.id, f.name, f.trigger_type, f.keyword, f.is_active, mb.username AS bot_username
               FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE f.id=$1 AND mb.added_by=$2""", funnel_id, uid)
        if not funnel:
            return _err("Funnel not found", 404)
        steps = await _safe_fetch(pool,
            "SELECT id, step_order, message_text, delay_minutes FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order",
            funnel_id)
        subs_active = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnel_subscriptions WHERE funnel_id=$1 AND completed=false", funnel_id)
        subs_total = await _safe_count(pool,
            "SELECT COUNT(*) FROM funnel_subscriptions WHERE funnel_id=$1", funnel_id)
        return _json_resp({
            "funnel": funnel,
            "steps": steps,
            "subs_active": subs_active,
            "subs_total": subs_total,
        })

    async def create_funnel(request: web.Request) -> web.Response:
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
        name = (body.get("name") or "").strip()
        trigger_type = body.get("trigger_type", "start")
        keyword = (body.get("keyword") or "").strip() or None
        first_message = (body.get("first_message") or "").strip()
        if not name:
            return _err("name required")
        if trigger_type not in ("start", "keyword"):
            return _err("trigger_type must be start or keyword")
        if trigger_type == "keyword" and not keyword:
            return _err("keyword required for keyword trigger")
        if not first_message:
            return _err("first_message required")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    funnel_row = await conn.fetchrow(
                        "INSERT INTO funnels(bot_id, name, trigger_type, keyword) VALUES($1,$2,$3,$4) RETURNING id",
                        bot_id, name, trigger_type, keyword)
                    funnel_id = funnel_row["id"]
                    await conn.execute(
                        "INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes) VALUES($1,1,$2,0)",
                        funnel_id, first_message)
            return _json_resp({"ok": True, "funnel_id": funnel_id})
        except Exception:
            log.exception("create_funnel bot=%d uid=%d", bot_id, uid)
            return _err("Failed to create funnel", 500)

    # ── Competitors ────────────────────────────────────────────────────────────

    async def competitors_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, username, label, channel_id, last_members, last_checked, created_at
               FROM competitors WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50""", uid)
        return _json_resp({"competitors": rows})

    async def add_competitor(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        username = (body.get("username") or "").strip().lstrip("@").lower()
        label = (body.get("label") or "").strip() or None
        if not username:
            return _err("username required")
        if len(username) > 50:
            return _err("username too long")
        try:
            row = await pool.fetchrow(
                """INSERT INTO competitors(owner_id, username, label) VALUES($1,$2,$3)
                   ON CONFLICT(owner_id, username) DO UPDATE SET label=EXCLUDED.label
                   RETURNING id""",
                uid, username, label)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("add_competitor uid=%d", uid)
            return _err("Failed to add competitor", 500)

    async def delete_competitor(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            comp_id = int(request.match_info["comp_id"])
        except (KeyError, ValueError):
            return _err("Invalid comp_id", 400)
        try:
            await pool.execute(
                "DELETE FROM competitors WHERE id=$1 AND owner_id=$2", comp_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete", 500)

    # ── Network Broadcast (all bots) ──────────────────────────────────────────

    async def network_broadcast(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        text = (body.get("text") or "").strip()
        if not text:
            return _err("text required")
        if len(text) > 4096:
            return _err("text too long (max 4096)")
        # Get all active bots with subscriber counts
        bots = await _safe_fetch(pool,
            """SELECT mb.bot_id, COUNT(bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE mb.added_by=$1 AND mb.is_active=true
               GROUP BY mb.bot_id""", uid)
        if not bots:
            return _err("No active bots found")
        total_recipients = sum(b["active_subs"] or 0 for b in bots)
        created_ids = []
        for b in bots:
            if not (b["active_subs"] or 0):
                continue
            try:
                row = await pool.fetchrow(
                    "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) VALUES($1,$2,$3,'pending',$4) RETURNING id",
                    b["bot_id"], text, b["active_subs"], uid)
                created_ids.append(row["id"])
            except Exception:
                log.warning("network_broadcast: failed bot=%d", b["bot_id"])
        return _json_resp({
            "ok": True,
            "broadcasts_created": len(created_ids),
            "total_recipients": total_recipients,
            "broadcast_ids": created_ids,
        })

    # ── CRM Contacts ─────────────────────────────────────────────────────────

    async def crm_contacts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        search = (request.query.get("q") or "").strip()
        tag = (request.query.get("tag") or "").strip()
        try:
            offset = max(0, int(request.query.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        if search:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1
                     AND (first_name ILIKE $2 OR last_name ILIKE $2 OR username ILIKE $2 OR phone ILIKE $2)
                   ORDER BY created_at DESC LIMIT 50 OFFSET $3""",
                uid, f"%{search}%", offset)
        elif tag:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1 AND $2=ANY(tags)
                   ORDER BY created_at DESC LIMIT 50 OFFSET $3""",
                uid, tag, offset)
        else:
            rows = await _safe_fetch(pool,
                """SELECT id, tg_user_id, username, first_name, last_name, phone,
                          tags, notes, source, created_at
                   FROM crm_contacts WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 50 OFFSET $2""",
                uid, offset)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1", uid)
        # Unique tags across all contacts
        tags_row = await _safe_fetch(pool,
            "SELECT DISTINCT unnest(tags) AS tag FROM crm_contacts WHERE owner_id=$1 ORDER BY tag", uid)
        all_tags = [r["tag"] for r in tags_row]
        return _json_resp({"contacts": rows, "total": total, "offset": offset, "all_tags": all_tags})

    async def bot_audience(request: web.Request) -> web.Response:
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
        total = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id)
        active = await _safe_count(pool, "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id)
        new_today = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '1 day'", bot_id)
        new_7d = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '7 days'", bot_id)
        new_30d = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '30 days'", bot_id)
        # Growth by day (last 14 days)
        growth = await _safe_fetch(pool,
            """SELECT DATE(first_seen) AS day, COUNT(*) AS new_users
               FROM bot_users WHERE bot_id=$1 AND first_seen >= now()-INTERVAL '14 days'
               GROUP BY day ORDER BY day""", bot_id)
        # Tags distribution
        tags = await _safe_fetch(pool,
            """SELECT tag, COUNT(*) AS cnt FROM user_tags WHERE bot_id=$1
               GROUP BY tag ORDER BY cnt DESC LIMIT 10""", bot_id)
        return _json_resp({
            "total": total, "active": active,
            "new_today": new_today, "new_7d": new_7d, "new_30d": new_30d,
            "growth": growth, "tags": tags,
        })

    # ── Keywords / Search Rankings ────────────────────────────────────────────

    async def keywords(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT tk.id, tk.keyword, mb.username AS bot_username, mb.bot_id,
                      tk.is_active, tk.created_at,
                      (SELECT sr.position FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_position,
                      (SELECT sr.checked_at FROM search_rankings sr
                       WHERE sr.keyword_id=tk.id ORDER BY sr.checked_at DESC LIMIT 1) AS last_checked
               FROM tracked_keywords tk
               JOIN managed_bots mb ON mb.bot_id=tk.bot_id
               WHERE tk.owner_id=$1
               ORDER BY tk.created_at DESC LIMIT 50""", uid)
        return _json_resp({"keywords": rows})

    async def add_keyword(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        bot_id = body.get("bot_id")
        keyword = (body.get("keyword") or "").strip().lower()
        if not bot_id or not keyword:
            return _err("bot_id and keyword required")
        if len(keyword) > 100:
            return _err("keyword too long")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id")
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id_int, uid)
        if not owns:
            return _err("Bot not found", 404)
        try:
            row = await pool.fetchrow(
                """INSERT INTO tracked_keywords(bot_id, owner_id, keyword)
                   VALUES($1,$2,$3) ON CONFLICT(bot_id, keyword) DO UPDATE SET is_active=true
                   RETURNING id""",
                bot_id_int, uid, keyword)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("add_keyword uid=%d", uid)
            return _err("Failed to add keyword", 500)

    async def delete_keyword(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            kw_id = int(request.match_info["kw_id"])
        except (KeyError, ValueError):
            return _err("Invalid kw_id", 400)
        try:
            await pool.execute(
                "DELETE FROM tracked_keywords WHERE id=$1 AND owner_id=$2", kw_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete", 500)

    # ── Account Warmup control ────────────────────────────────────────────────

    async def start_warmup(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Account not found", 404)
        plan_type = body.get("plan_type", "standard")
        if plan_type not in ("standard", "gentle", "aggressive"):
            plan_type = "standard"
        target_days = {"standard": 14, "gentle": 21, "aggressive": 7}[plan_type]
        daily_actions = {"standard": 10, "gentle": 5, "aggressive": 20}[plan_type]
        # Cancel any active warmup first
        try:
            await pool.execute(
                "UPDATE account_warmup_plans SET status='paused' WHERE account_id=$1 AND status='active'",
                acc_id)
            row = await pool.fetchrow(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5) RETURNING id""",
                uid, acc_id, plan_type, target_days, daily_actions)
            return _json_resp({"ok": True, "id": row["id"], "target_days": target_days})
        except Exception:
            log.exception("start_warmup acc=%d uid=%d", acc_id, uid)
            return _err("Failed to start warmup", 500)

    async def pause_warmup(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Invalid acc_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans SET status='paused'
                   WHERE account_id=$1 AND owner_id=$2 AND status='active'
                   RETURNING id""", acc_id, uid)
            if not row:
                return _err("No active warmup", 404)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to pause", 500)

    # ── Schedules ────────────────────────────────────────────────────────────

    async def bot_schedules(request: web.Request) -> web.Response:
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
            """SELECT id, message_text, execute_at, status, created_at
               FROM scheduled_broadcasts WHERE bot_id=$1
               ORDER BY execute_at DESC LIMIT 20""", bot_id)
        return _json_resp({"schedules": rows})

    async def create_schedule(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        text = (body.get("text") or "").strip()
        execute_at = (body.get("execute_at") or "").strip()
        if not text:
            return _err("text required")
        if not execute_at:
            return _err("execute_at required (ISO datetime)")
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(execute_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                import datetime as _dt
                dt = dt.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            return _err("execute_at must be ISO datetime (e.g. 2025-12-31T15:00:00Z)")
        try:
            row = await pool.fetchrow(
                "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by) VALUES($1,$2,$3,$4) RETURNING id",
                bot_id, text, dt, uid)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("create_schedule bot=%d uid=%d", bot_id, uid)
            return _err("Failed to create schedule", 500)

    async def cancel_schedule(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sch_id = int(request.match_info["sch_id"])
        except (KeyError, ValueError):
            return _err("Invalid sch_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE scheduled_broadcasts SET status='cancelled'
                   WHERE id=$1 AND created_by=$2 AND status='pending'
                   RETURNING id""", sch_id, uid)
            if not row:
                return _err("Not found or already done", 404)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to cancel", 500)

    # ── Templates ────────────────────────────────────────────────────────────

    async def templates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, name, asset_type, template, created_at
               FROM asset_templates WHERE owner_id=$1 AND asset_type='post'
               ORDER BY created_at DESC LIMIT 30""", uid)
        for r in rows:
            if isinstance(r.get("template"), str):
                import json as _json
                try:
                    r["template"] = _json.loads(r["template"])
                except Exception:
                    r["template"] = {}
        return _json_resp({"templates": rows})

    async def create_template(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        name = (body.get("name") or "").strip()
        text = (body.get("text") or "").strip()
        if not name:
            return _err("name required")
        if not text:
            return _err("text required")
        if len(text) > 4096:
            return _err("text too long (max 4096)")
        import json as _json
        try:
            row = await pool.fetchrow(
                "INSERT INTO asset_templates(owner_id, asset_type, name, template) VALUES($1,'post',$2,$3::jsonb) RETURNING id",
                uid, name, _json.dumps({"text": text}))
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("create_template uid=%d", uid)
            return _err("Failed to create template", 500)

    async def delete_template(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("Invalid tpl_id", 400)
        try:
            await pool.execute(
                "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete", 500)

    # ── Mass Publish ─────────────────────────────────────────────────────────

    async def mass_publish(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        text = (body.get("text") or "").strip()
        delay = int(body.get("delay", 30))
        channel_ids = body.get("channel_ids")  # optional list; None = all channels
        if not text:
            return _err("text required")
        if len(text) > 4096:
            return _err("text too long (max 4096)")
        if delay not in (5, 30, 60, -1):
            delay = 30
        # Count target channels
        if channel_ids:
            total = len(channel_ids)
        else:
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
        if total == 0:
            return _err("No channels to publish to")
        params = {"text": text, "delay": delay, "owner_id": uid}
        if channel_ids:
            params["channel_ids"] = channel_ids
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "mass_publish", params, total_items=total)
            return _json_resp({"ok": True, "op_id": op_id, "total": total})
        except Exception:
            log.exception("mass_publish uid=%d", uid)
            return _err("Failed to enqueue mass publish", 500)

    # ── Proxies ──────────────────────────────────────────────────────────────

    async def proxies(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT id, label, proxy_url, proxy_type, is_active, is_alive, last_check, created_at
               FROM user_proxies WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50""", uid)
        return _json_resp({"proxies": rows})

    async def add_proxy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        proxy_url = (body.get("proxy_url") or "").strip()
        label = (body.get("label") or "").strip() or None
        if not proxy_url:
            return _err("proxy_url required")
        import re as _re
        if not _re.match(r"^(socks5|socks4|http)://", proxy_url, _re.IGNORECASE):
            return _err("proxy_url must start with socks5://, socks4://, or http://")
        proxy_type = "socks5"
        if proxy_url.lower().startswith("http://"):
            proxy_type = "http"
        elif proxy_url.lower().startswith("socks4://"):
            proxy_type = "socks4"
        try:
            row = await pool.fetchrow(
                """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type)
                   VALUES($1,$2,$3,$4) ON CONFLICT(owner_id, proxy_url) DO UPDATE
                   SET label=EXCLUDED.label RETURNING id""",
                uid, label, proxy_url, proxy_type)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("add_proxy uid=%d", uid)
            return _err("Failed to add proxy", 500)

    async def delete_proxy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Invalid proxy_id", 400)
        try:
            await pool.execute(
                "DELETE FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete proxy", 500)

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
    # Channels
    app.router.add_get("/api/miniapp/channel/{ch_id}", channel_detail)
    app.router.add_post("/api/miniapp/channel/{ch_id}/post", post_to_channel)
    # Bot subscribers
    app.router.add_get("/api/miniapp/bot/{bot_id}/subscribers", bot_subscribers)
    # DM campaigns
    app.router.add_post("/api/miniapp/dm_campaign", create_dm_campaign)
    # Operations
    app.router.add_get("/api/miniapp/operations", operations)
    app.router.add_post("/api/miniapp/operation/{op_id}/cancel", cancel_operation)
    # Bot toggle
    app.router.add_put("/api/miniapp/bot/{bot_id}/toggle", toggle_bot)
    # Funnel steps
    app.router.add_get("/api/miniapp/funnel/{funnel_id}/steps", funnel_steps)
    app.router.add_post("/api/miniapp/bot/{bot_id}/funnel", create_funnel)
    # Competitors
    app.router.add_get("/api/miniapp/competitors", competitors_list)
    app.router.add_post("/api/miniapp/competitor", add_competitor)
    app.router.add_delete("/api/miniapp/competitor/{comp_id}", delete_competitor)
    # Network broadcast
    app.router.add_post("/api/miniapp/network_broadcast", network_broadcast)
    # CRM
    app.router.add_get("/api/miniapp/crm/contacts", crm_contacts)
    # Audience
    app.router.add_get("/api/miniapp/bot/{bot_id}/audience", bot_audience)
    # Keywords / Search Rankings
    app.router.add_get("/api/miniapp/keywords", keywords)
    app.router.add_post("/api/miniapp/keyword", add_keyword)
    app.router.add_delete("/api/miniapp/keyword/{kw_id}", delete_keyword)
    # Account Warmup control
    app.router.add_post("/api/miniapp/account/{acc_id}/warmup/start", start_warmup)
    app.router.add_post("/api/miniapp/account/{acc_id}/warmup/pause", pause_warmup)
    # Schedules
    app.router.add_get("/api/miniapp/bot/{bot_id}/schedules", bot_schedules)
    app.router.add_post("/api/miniapp/bot/{bot_id}/schedule", create_schedule)
    app.router.add_post("/api/miniapp/schedule/{sch_id}/cancel", cancel_schedule)
    # Templates
    app.router.add_get("/api/miniapp/templates", templates)
    app.router.add_post("/api/miniapp/template", create_template)
    app.router.add_delete("/api/miniapp/template/{tpl_id}", delete_template)
    # Mass Publish
    app.router.add_post("/api/miniapp/mass_publish", mass_publish)
    # Proxies
    app.router.add_get("/api/miniapp/proxies", proxies)
    app.router.add_post("/api/miniapp/proxy", add_proxy)
    app.router.add_delete("/api/miniapp/proxy/{proxy_id}", delete_proxy)
    # Analytics
    app.router.add_get("/api/miniapp/analytics", analytics)
    # Subscription
    app.router.add_get("/api/miniapp/subscription", subscription)
    app.router.add_get("/api/miniapp/referral", referral)
    # Deeplinks
    app.router.add_get("/api/miniapp/bot/{bot_id}/deeplinks", bot_deeplinks)
    app.router.add_post("/api/miniapp/bot/{bot_id}/deeplink", create_deeplink)
    app.router.add_delete("/api/miniapp/deeplink/{link_id}", delete_deeplink)
    # Engagement segments
    app.router.add_get("/api/miniapp/bot/{bot_id}/engagement", bot_engagement)
    # Bot Stats (detailed)
    app.router.add_get("/api/miniapp/bot/{bot_id}/stats", bot_stats)
    # Profile Setter
    app.router.add_get("/api/miniapp/profile_setter/status", profile_setter_status)
    app.router.add_post("/api/miniapp/profile_setter", profile_setter_submit)
    # Bot Notes
    app.router.add_get("/api/miniapp/bot/{bot_id}/note", bot_note)
    app.router.add_put("/api/miniapp/bot/{bot_id}/note", save_bot_note)
    # Bot Commands
    app.router.add_get("/api/miniapp/bot/{bot_id}/commands", bot_commands)
    app.router.add_put("/api/miniapp/bot/{bot_id}/commands", set_bot_commands)
    # Relay (Inbox)
    app.router.add_get("/api/miniapp/bot/{bot_id}/relay/sessions", relay_sessions_list)
    app.router.add_get("/api/miniapp/relay/session/{session_id}/messages", relay_session_messages)
    app.router.add_put("/api/miniapp/bot/{bot_id}/relay/toggle", relay_toggle)
    # API Keys
    app.router.add_get("/api/miniapp/api_keys", api_keys_list)
    app.router.add_delete("/api/miniapp/api_key/{key_id}", revoke_api_key)
    # Strike history
    app.router.add_get("/api/miniapp/strike/history", strike_history)
    # Audience Parser
    app.router.add_get("/api/miniapp/parser/runs", parser_runs)
    app.router.add_get("/api/miniapp/parser/audience", parsed_audience)
    app.router.add_post("/api/miniapp/parser/submit", submit_parse_job)
    # CRM Deals
    app.router.add_get("/api/miniapp/crm/deals", crm_deals)
    app.router.add_post("/api/miniapp/crm/deal", create_crm_deal)
    app.router.add_put("/api/miniapp/crm/deal/{deal_id}/stage", update_crm_deal_stage)
    app.router.add_delete("/api/miniapp/crm/deal/{deal_id}", delete_crm_deal)
    # Workspaces
    app.router.add_get("/api/miniapp/workspaces", workspaces_list)
    app.router.add_post("/api/miniapp/workspace", create_workspace)
    app.router.add_delete("/api/miniapp/workspace/{ws_id}", leave_workspace)
    # Promo Platform
    app.router.add_get("/api/miniapp/promo", promo_overview)
    app.router.add_post("/api/miniapp/promo/order", promo_create_order_api)
    app.router.add_post("/api/miniapp/promo/order/{order_id}/cancel", promo_cancel_order)
    app.router.add_post("/api/miniapp/promo/warehouse/bot", promo_add_warehouse_bot)
    # Error Reports
    app.router.add_post("/api/miniapp/error_report", submit_error_report)
    app.router.add_get("/api/miniapp/error_reports", my_error_reports)
    # Account Cleaner
    app.router.add_get("/api/miniapp/cleaner/accounts", cleaner_accounts)
    app.router.add_post("/api/miniapp/cleaner/submit", cleaner_submit)
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
