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
            """SELECT channel_id AS id, channel_id, username, title,
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

    # ── Topology Map ──────────────────────────────────────────────────────────

    async def topology_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            accs = await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", uid)
            channels = await pool.fetchval("SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
            bots = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
            # Channel-to-account links via joined_channels (table may not exist)
            try:
                links = await pool.fetchval(
                    """SELECT COUNT(*) FROM joined_channels jc
                       JOIN tg_accounts ta ON ta.id=jc.account_id
                       WHERE ta.owner_id=$1""",
                    uid,
                )
            except Exception:
                links = 0
            # Bot-user relationships
            try:
                bot_users_total = await pool.fetchval(
                    """SELECT COUNT(*) FROM bot_users bu
                       JOIN managed_bots b ON b.bot_id=bu.bot_id
                       WHERE b.added_by=$1""",
                    uid,
                )
            except Exception:
                bot_users_total = 0
            return _json_resp({
                "accounts": accs, "channels": channels, "bots": bots,
                "channel_links": links, "bot_users_total": bot_users_total,
            })
        except Exception as exc:
            log.exception("topology_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Infra Analytics ────────────────────────────────────────────────────────

    async def infra_analytics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            active_accs = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE", uid
            )
            flood_24h = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log fl
                   JOIN tg_accounts ta ON ta.id=fl.account_id
                   WHERE ta.owner_id=$1 AND fl.created_at > NOW()-INTERVAL '24h'""",
                uid,
            )
            ops_24h = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '24h'",
                uid,
            )
            warmup_active = await pool.fetchval(
                """SELECT COUNT(*) FROM account_warmup_plans wp
                   WHERE wp.owner_id=$1 AND wp.status='active'""",
                uid,
            )
            pools = await pool.fetch(
                """SELECT pool, COUNT(*) AS cnt FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND pool IS NOT NULL
                   GROUP BY pool ORDER BY cnt DESC LIMIT 10""",
                uid,
            )
            # Recent audit
            audit = await pool.fetch(
                """SELECT action, target, result, occurred_at
                   FROM operation_audit WHERE owner_id=$1
                   ORDER BY occurred_at DESC LIMIT 10""",
                uid,
            )
            return _json_resp({
                "active_accounts": active_accs,
                "flood_24h": flood_24h,
                "ops_24h": ops_24h,
                "warmup_active": warmup_active,
                "pools": [dict(p) for p in pools],
                "audit": [
                    {**dict(a), "occurred_at": a["occurred_at"].isoformat() if a["occurred_at"] else None}
                    for a in audit
                ],
            })
        except Exception as exc:
            log.exception("infra_analytics_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Reporter (Report users) ────────────────────────────────────────────────

    async def reporter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        target = (body.get("target") or "").strip()
        reason = body.get("reason", "spam")
        acc_count = int(body.get("acc_count", 5))
        if not target:
            return _err("Укажите цель репортинга", 400)
        try:
            label = f"Репорт {target} ({reason}) × {acc_count} аккаунтов"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'report_peer','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"target": target, "reason": reason}), acc_count, label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("reporter_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Quick Post ─────────────────────────────────────────────────────────────

    async def quick_post_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        text = (body.get("text") or "").strip()
        channel_ids = body.get("channel_ids") or []
        if not text:
            return _err("Заполните текст поста", 400)
        if not channel_ids:
            return _err("Выберите хотя бы один канал", 400)
        try:
            label = f"Quick Post в {len(channel_ids)} каналов"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'quick_post','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"text": text, "channel_ids": channel_ids}), len(channel_ids), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("quick_post_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── SEO Overview ───────────────────────────────────────────────────────────

    async def seo_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            suggestions = await pool.fetch(
                """SELECT s.owner_id, s.chan_id, s.title, s.about, s.username, s.created_at,
                          c.title AS channel_title, c.username AS channel_username
                   FROM seo_ai_suggestions s
                   LEFT JOIN managed_channels c ON c.id=s.chan_id
                   WHERE s.owner_id=$1 ORDER BY s.created_at DESC LIMIT 20""",
                uid,
            )
            keywords = await pool.fetch(
                "SELECT keyword, search_count FROM search_memory WHERE owner_id=$1 ORDER BY search_count DESC LIMIT 20",
                uid,
            )
            return _json_resp({
                "suggestions": [
                    {**dict(s), "created_at": s["created_at"].isoformat() if s["created_at"] else None}
                    for s in suggestions
                ],
                "keywords": [dict(k) for k in keywords],
            })
        except Exception as exc:
            log.exception("seo_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Bot Factory Overview ───────────────────────────────────────────────────

    async def bot_factory_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
            active = await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE", uid)
            recent = await pool.fetch(
                """SELECT bot_id, username, first_name, is_active, added_at
                   FROM managed_bots WHERE added_by=$1
                   ORDER BY added_at DESC LIMIT 10""",
                uid,
            )
            return _json_resp({
                "total": total, "active": active,
                "recent": [
                    {**dict(r), "added_at": r["added_at"].isoformat() if r["added_at"] else None}
                    for r in recent
                ],
            })
        except Exception as exc:
            log.exception("bot_factory_status uid=%d", uid)
            return _err(str(exc), 500)

    # ── Persona Hub ───────────────────────────────────────────────────────────

    async def persona_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT pp.id, pp.persona_name, pp.bio, pp.age, pp.speech_style,
                          pp.tone, pp.niche, pp.is_active, pp.created_at,
                          ta.phone, ta.first_name, ta.username
                   FROM persona_profiles pp
                   LEFT JOIN tg_accounts ta ON ta.id=pp.account_id
                   WHERE pp.owner_id=$1 ORDER BY pp.created_at DESC""",
                uid,
            )
            return _json_resp({"personas": [
                {
                    **dict(r),
                    "interests": list(r.get("interests") or []),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in rows
            ]})
        except Exception as exc:
            log.exception("persona_list uid=%d", uid)
            return _err(str(exc), 500)

    async def persona_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        persona_id = int(request.match_info["persona_id"])
        row = await pool.fetchrow(
            "SELECT id, is_active FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
        )
        if not row:
            return _err("Не найдено", 404)
        new_val = not row["is_active"]
        await pool.execute(
            "UPDATE persona_profiles SET is_active=$1 WHERE id=$2", new_val, persona_id
        )
        return _json_resp({"is_active": new_val})

    async def persona_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        persona_id = int(request.match_info["persona_id"])
        await pool.execute(
            "DELETE FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
        )
        return _json_resp({"ok": True})

    async def persona_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        name = (body.get("persona_name") or "").strip()
        if not name:
            return _err("Имя персоны обязательно", 400)
        VALID_STYLES = {"formal", "casual", "expert", "friendly", "sarcastic", "neutral"}
        speech_style = body.get("speech_style", "casual")
        if speech_style not in VALID_STYLES:
            speech_style = "casual"
        try:
            interests_raw = body.get("interests", "")
            if isinstance(interests_raw, list):
                interests = [str(i).strip() for i in interests_raw if str(i).strip()]
            else:
                interests = [t.strip() for t in str(interests_raw).split(",") if t.strip()]
            age = int(body.get("age") or 25)
            age = max(18, min(80, age))
            row = await pool.fetchrow(
                """INSERT INTO persona_profiles
                   (owner_id, persona_name, bio, age, interests, speech_style, tone, niche, backstory, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                   RETURNING id, persona_name, is_active""",
                uid, name,
                (body.get("bio") or "").strip() or None,
                age,
                interests,
                speech_style,
                (body.get("tone") or "positive").strip(),
                (body.get("niche") or "").strip() or None,
                (body.get("backstory") or "").strip() or None,
            )
            return _json_resp({"ok": True, "persona": dict(row)})
        except Exception:
            log.exception("persona_create uid=%d", uid)
            return _err("Ошибка создания персоны", 500)

    # ── Auto Registrar ─────────────────────────────────────────────────────────

    async def autoreg_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1", uid
            )
            recent = await pool.fetch(
                """SELECT id, phone, first_name, acc_status, added_at
                   FROM tg_accounts WHERE owner_id=$1
                   ORDER BY added_at DESC LIMIT 10""",
                uid,
            )
            pending_ops = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND op_type='auto_register' AND status='pending'",
                uid,
            )
            return _json_resp({
                "total_accounts": total,
                "pending_registrations": pending_ops,
                "recent": [
                    {**dict(r), "added_at": r["added_at"].isoformat() if r["added_at"] else None}
                    for r in recent
                ],
            })
        except Exception as exc:
            log.exception("autoreg_status uid=%d", uid)
            return _err(str(exc), 500)

    async def autoreg_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        count = int(body.get("count", 1))
        country = body.get("country", "RU")
        service = body.get("service", "smsactivate")
        if count < 1 or count > 50:
            return _err("Количество: от 1 до 50", 400)
        try:
            label = f"Авторег {count} аккаунт(ов) · {country}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'auto_register','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"count": count, "country": country, "service": service}), count, label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("autoreg_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Phone Checker ─────────────────────────────────────────────────────────

    async def phone_check_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        phones_raw = (body.get("phones") or "").strip()
        if not phones_raw:
            return _err("Укажите номера телефонов", 400)
        phones = [p.strip() for p in phones_raw.replace(",", "\n").split("\n") if p.strip()]
        if not phones:
            return _err("Нет валидных номеров", 400)
        try:
            label = f"Проверка {len(phones)} номеров"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'phone_check','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"phones": phones}), len(phones), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "count": len(phones)})
        except Exception as exc:
            log.exception("phone_check_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Referral Dashboard ────────────────────────────────────────────────────

    async def referral_overview_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ref_code_row = await pool.fetchrow(
                "SELECT code FROM platform_referral_codes WHERE user_id=$1", uid
            )
            amb_row = await pool.fetchrow(
                """SELECT a.tier_key, a.total_commission,
                          COALESCE(t.commission_pct, 0) AS commission_rate,
                          COALESCE(t.tier_name, 'Базовый') AS tier_name
                   FROM ambassador_status a
                   LEFT JOIN ambassador_tiers t ON t.tier_key = a.tier_key
                   WHERE a.user_id=$1""",
                uid,
            )
            referral_count = await pool.fetchval(
                "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", uid
            )
            paid_count = await pool.fetchval(
                """SELECT COUNT(*) FROM platform_referrals r
                   WHERE r.referrer_id=$1 AND r.paid_at IS NOT NULL""",
                uid,
            )
            top_refs = await pool.fetch(
                """SELECT u.user_id AS id, u.username, r.created_at, r.activated_at
                   FROM platform_referrals r
                   JOIN platform_users u ON u.user_id=r.referred_id
                   WHERE r.referrer_id=$1
                   ORDER BY r.created_at DESC LIMIT 20""",
                uid,
            )
            return _json_resp({
                "ref_code": ref_code_row["code"] if ref_code_row else None,
                "tier": amb_row["tier_key"] if amb_row else "basic",
                "tier_name": amb_row["tier_name"] if amb_row else "Базовый",
                "commission_rate": float(amb_row["commission_rate"] or 0) if amb_row else 0,
                "total_earned": float(amb_row["total_commission"] or 0) if amb_row else 0,
                "referral_count": int(referral_count or 0),
                "paid_count": int(paid_count or 0),
                "referrals": [
                    {
                        "id": r["id"], "username": r["username"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "status": "active" if r["activated_at"] else "pending",
                    }
                    for r in top_refs
                ],
            })
        except Exception as exc:
            log.exception("referral_overview_detail uid=%d", uid)
            return _err(str(exc), 500)

    # ── AI Memory ─────────────────────────────────────────────────────────────

    async def ai_memory_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, kind, title, body, tags, pinned, created_at, updated_at
                   FROM botmother_memory WHERE owner_id=$1
                   ORDER BY pinned DESC, updated_at DESC LIMIT 50""",
                uid,
            )
            return _json_resp({"memories": [
                {
                    **dict(r),
                    "tags": list(r["tags"] or []),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]})
        except Exception as exc:
            log.exception("ai_memory_list uid=%d", uid)
            return _err(str(exc), 500)

    async def ai_memory_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        title = (body.get("title") or "").strip()
        mem_body = (body.get("body") or "").strip()
        if not mem_body:
            return _err("Заполните содержимое", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO botmother_memory(owner_id, kind, title, body, source)
                   VALUES($1,'note',$2,$3,'miniapp') RETURNING id""",
                uid, title, mem_body,
            )
            return _json_resp({"id": row["id"]})
        except Exception as exc:
            log.exception("ai_memory_create uid=%d", uid)
            return _err(str(exc), 500)

    async def ai_memory_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        mem_id = int(request.match_info["mem_id"])
        await pool.execute(
            "DELETE FROM botmother_memory WHERE id=$1 AND owner_id=$2", mem_id, uid
        )
        return _json_resp({"ok": True})

    # ── Nodes Hub (Forum Workspaces) ───────────────────────────────────────────

    async def nodes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nodes = await pool.fetch(
                """SELECT n.id, n.tg_chat_id, n.node_type, n.name, n.is_active, n.created_at,
                          COUNT(t.id) FILTER (WHERE t.status='open') AS open_threads,
                          COUNT(t.id) AS total_threads
                   FROM bm_telegram_nodes n
                   LEFT JOIN bm_node_threads t ON t.node_id=n.id
                   WHERE n.owner_id=$1
                   GROUP BY n.id ORDER BY n.created_at DESC""",
                uid,
            )
            return _json_resp({"nodes": [
                {**dict(n), "created_at": n["created_at"].isoformat() if n["created_at"] else None}
                for n in nodes
            ]})
        except Exception as exc:
            log.exception("nodes_list uid=%d", uid)
            return _err(str(exc), 500)

    async def node_threads(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        node_id = int(request.match_info["node_id"])
        try:
            node = await pool.fetchrow(
                "SELECT id, name FROM bm_telegram_nodes WHERE id=$1 AND owner_id=$2",
                node_id, uid,
            )
            if not node:
                return _err("Не найдено", 404)
            threads = await pool.fetch(
                """SELECT id, entity_type, entity_id, topic_name, status, created_at
                   FROM bm_node_threads WHERE node_id=$1 ORDER BY status, created_at DESC""",
                node_id,
            )
            return _json_resp({
                "node": dict(node),
                "threads": [
                    {**dict(t), "created_at": t["created_at"].isoformat() if t["created_at"] else None}
                    for t in threads
                ],
            })
        except Exception as exc:
            log.exception("node_threads uid=%d node=%d", uid, node_id)
            return _err(str(exc), 500)

    # ── Gift Transfer ──────────────────────────────────────────────────────────

    async def gift_inventory(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            table_exists = await pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='gift_inventory')"
            )
            if not table_exists:
                return _json_resp({"total": 0, "transferable": 0, "items": [], "note": "Таблица подарков ещё не создана. Запустите сканирование через бота."})
            items = await pool.fetch(
                """SELECT gi.id, gi.gift_type, gi.stars_cost, gi.is_transferable,
                          gi.is_unique, gi.is_premium, gi.scanned_at,
                          ta.phone, ta.first_name
                   FROM gift_inventory gi
                   JOIN tg_accounts ta ON ta.id=gi.account_id
                   WHERE gi.owner_id=$1
                   ORDER BY gi.scanned_at DESC LIMIT 100""",
                uid,
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1", uid
            )
            transferable = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1 AND is_transferable=TRUE", uid
            )
            return _json_resp({
                "total": total, "transferable": transferable,
                "items": [
                    {**dict(i), "scanned_at": i["scanned_at"].isoformat() if i.get("scanned_at") else None}
                    for i in items
                ],
            })
        except Exception as exc:
            log.exception("gift_inventory uid=%d", uid)
            return _err(str(exc), 500)

    async def gift_scan_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            label = "Сканирование подарков во всех аккаунтах"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'gift_scan','pending','{}',1,$2) RETURNING id",
                uid, label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("gift_scan_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Mass Inviter ───────────────────────────────────────────────────────────

    async def mass_inviter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        group = (body.get("group") or "").strip()
        if not group:
            return _err("Укажите группу/канал", 400)
        source = body.get("source", "parsed")
        try:
            label = f"Mass Invite → {group}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'mass_invite','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"group": group, "source": source}), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("mass_inviter_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Stars Hub ─────────────────────────────────────────────────────────────

    async def stars_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            summary = await pool.fetchrow(
                """SELECT
                   COALESCE(SUM(revenue_a + revenue_b), 0) AS total_revenue,
                   COALESCE(SUM(conversions_a + conversions_b), 0) AS total_conversions,
                   COUNT(*) FILTER (WHERE status='active') AS active_exps,
                   COUNT(*) AS total_exps
                   FROM stars_experiments WHERE owner_id=$1""",
                uid,
            )
            exps = await pool.fetch(
                """SELECT id, name, status, winner, price_a, price_b,
                          impressions_a, conversions_a, revenue_a,
                          impressions_b, conversions_b, revenue_b,
                          created_at
                   FROM stars_experiments WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 20""",
                uid,
            )
            return _json_resp({
                "total_revenue": summary["total_revenue"],
                "total_conversions": summary["total_conversions"],
                "active_exps": summary["active_exps"],
                "total_exps": summary["total_exps"],
                "experiments": [
                    {**dict(e), "created_at": e["created_at"].isoformat() if e["created_at"] else None}
                    for e in exps
                ],
            })
        except Exception as exc:
            log.exception("stars_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Ghost Engine ───────────────────────────────────────────────────────────

    async def ghost_profiles(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT gp.id, gp.personality, gp.enabled, gp.daily_cap,
                          gp.active_hours_start, gp.active_hours_end, gp.created_at,
                          ta.phone, ta.first_name, ta.username
                   FROM ghost_profiles gp
                   JOIN tg_accounts ta ON ta.id = gp.account_id
                   WHERE gp.owner_id=$1 ORDER BY gp.created_at DESC""",
                uid,
            )
            return _json_resp({"profiles": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception as exc:
            log.exception("ghost_profiles uid=%d", uid)
            return _err(str(exc), 500)

    async def ghost_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        profile_id = int(request.match_info["profile_id"])
        row = await pool.fetchrow(
            "SELECT id, enabled FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
        )
        if not row:
            return _err("Не найдено", 404)
        new_val = not row["enabled"]
        await pool.execute(
            "UPDATE ghost_profiles SET enabled=$1, updated_at=now() WHERE id=$2", new_val, profile_id
        )
        return _json_resp({"enabled": new_val})

    async def ghost_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        profile_id = int(request.match_info["profile_id"])
        await pool.execute(
            "DELETE FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
        )
        return _json_resp({"ok": True})

    # ── Bot Webhook ────────────────────────────────────────────────────────────

    async def bot_webhook_info(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bot_id = int(request.match_info["bot_id"])
        row = await pool.fetchrow(
            "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id, uid,
        )
        if not row:
            return _err("Бот не найден", 404)
        try:
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                from services import bot_api as _bapi
                info = await _bapi.get_webhook_info(sess, row["token"])
            return _json_resp({
                "url": info.get("url", "") or "",
                "pending_update_count": info.get("pending_update_count", 0),
                "last_error_message": info.get("last_error_message", ""),
                "max_connections": info.get("max_connections", 0),
                "allowed_updates": info.get("allowed_updates", []),
                "bot_username": row["username"],
            })
        except Exception as exc:
            log.exception("bot_webhook_info uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    async def bot_webhook_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        bot_id = int(request.match_info["bot_id"])
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
        )
        if not row:
            return _err("Бот не найден", 404)
        try:
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                from services import bot_api as _bapi
                result = await _bapi.delete_webhook(sess, row["token"])
            return _json_resp({"ok": result.get("ok", False)})
        except Exception as exc:
            log.exception("bot_webhook_delete uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    # ── Reg Checker (Registration Date) ───────────────────────────────────────

    async def reg_check_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT entity_id, entity_type, entity_name, username,
                          reg_date, method, checked_at
                   FROM reg_check_cache WHERE checked_by=$1
                   ORDER BY checked_at DESC LIMIT 30""",
                uid,
            )
            return _json_resp({"checks": [
                {
                    **dict(r),
                    "reg_date": r["reg_date"].isoformat() if r["reg_date"] else None,
                    "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
                }
                for r in rows
            ]})
        except Exception as exc:
            log.exception("reg_check_history uid=%d", uid)
            return _err(str(exc), 500)

    async def reg_check_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        target = (body.get("target") or "").strip()
        if not target:
            return _err("Укажите цель проверки", 400)
        try:
            label = f"Проверка даты регистрации: {target}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'reg_check','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"target": target}), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("reg_check_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── DM Campaigns ───────────────────────────────────────────────────────────

    async def dm_campaigns_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, name, status, sent_count, fail_count, total_targets,
                          text_template, target_type, created_at
                   FROM dm_campaigns WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 50""",
                uid,
            )
            return _json_resp({"campaigns": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception as exc:
            log.exception("dm_campaigns_list uid=%d", uid)
            return _err(str(exc), 500)

    async def dm_campaign_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        name = (body.get("name") or "").strip()
        text = (body.get("text") or "").strip()
        target_type = body.get("target_type", "bot_users")
        target_id = body.get("target_id")
        if not name or not text:
            return _err("Заполните название и текст", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id)
                   VALUES($1,$2,$3,$4,$5) RETURNING id""",
                uid, name, text, target_type, target_id,
            )
            return _json_resp({"id": row["id"]})
        except Exception as exc:
            log.exception("dm_campaign_create uid=%d", uid)
            return _err(str(exc), 500)

    async def dm_campaign_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        campaign_id = int(request.match_info["campaign_id"])
        row = await pool.fetchrow(
            "SELECT id, name FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            campaign_id, uid,
        )
        if not row:
            return _err("Не найдено", 404)
        try:
            label = f"DM-кампания: {row['name']}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'dm_campaign','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"campaign_id": campaign_id}), label,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET status='running', started_at=now() WHERE id=$1", campaign_id
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("dm_campaign_launch uid=%d cid=%d", uid, campaign_id)
            return _err(str(exc), 500)

    async def dm_campaign_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        campaign_id = int(request.match_info["campaign_id"])
        await pool.execute(
            "DELETE FROM dm_campaigns WHERE id=$1 AND owner_id=$2", campaign_id, uid
        )
        return _json_resp({"ok": True})

    # ── Account Warmup ─────────────────────────────────────────────────────────

    async def warmup_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plans = await pool.fetch(
                """SELECT wp.id, wp.plan_type, wp.current_day, wp.target_days,
                          wp.daily_actions, wp.status, wp.started_at,
                          ta.phone, ta.first_name, ta.username
                   FROM account_warmup_plans wp
                   JOIN tg_accounts ta ON ta.id = wp.account_id
                   WHERE wp.owner_id=$1
                   ORDER BY wp.started_at DESC LIMIT 50""",
                uid,
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1", uid
            )
            active = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1 AND status='active'", uid
            )
            return _json_resp({
                "total": total, "active": active,
                "plans": [
                    {**dict(p), "started_at": p["started_at"].isoformat() if p["started_at"] else None}
                    for p in plans
                ],
            })
        except Exception as exc:
            log.exception("warmup_overview uid=%d", uid)
            return _err(str(exc), 500)

    async def warmup_create_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        account_id = body.get("account_id")
        plan_type = body.get("plan_type", "standard")
        if not account_id:
            return _err("account_id обязателен", 400)
        acc = await pool.fetchrow(
            "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2", account_id, uid
        )
        if not acc:
            return _err("Аккаунт не найден", 404)
        try:
            days_map = {"gentle": 21, "standard": 14, "aggressive": 10}
            actions_map = {"gentle": 5, "standard": 10, "aggressive": 12}
            target_days = days_map.get(plan_type, 14)
            daily_actions = actions_map.get(plan_type, 10)
            label = f"Прогрев аккаунта ({plan_type})"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'account_warmup','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"account_id": account_id, "plan_type": plan_type}), label,
            )
            await pool.execute(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (account_id) DO UPDATE
                   SET plan_type=$3, target_days=$4, daily_actions=$5, status='active', started_at=now()""",
                uid, account_id, plan_type, target_days, daily_actions,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("warmup_create_plan uid=%d acc=%s", uid, account_id)
            return _err(str(exc), 500)

    async def warmup_delete_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        plan_id = int(request.match_info["plan_id"])
        await pool.execute(
            "DELETE FROM account_warmup_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
        )
        return _json_resp({"ok": True})

    # ── A/B Experiments ────────────────────────────────────────────────────────

    async def experiments_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT e.id, e.name, e.experiment_type, e.status, e.created_at,
                          b.username AS bot_username, b.first_name AS bot_name, e.bot_id,
                          (SELECT COUNT(*) FROM experiment_variants ev WHERE ev.experiment_id=e.id) AS variant_count
                   FROM experiments e
                   JOIN managed_bots b ON b.bot_id = e.bot_id
                   WHERE b.added_by=$1
                   ORDER BY e.created_at DESC LIMIT 50""",
                uid,
            )
            return _json_resp({"experiments": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception as exc:
            log.exception("experiments_list uid=%d", uid)
            return _err(str(exc), 500)

    async def experiment_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        exp_id = int(request.match_info["exp_id"])
        try:
            exp = await pool.fetchrow(
                """SELECT e.id, e.name, e.experiment_type, e.status, e.created_at,
                          e.winner_variant_id, e.min_sample_size, b.username AS bot_username
                   FROM experiments e
                   JOIN managed_bots b ON b.bot_id=e.bot_id
                   WHERE e.id=$1 AND b.added_by=$2""",
                exp_id, uid,
            )
            if not exp:
                return _err("Не найдено", 404)
            variants = await pool.fetch(
                "SELECT id, name, content, weight, impressions, conversions FROM experiment_variants WHERE experiment_id=$1 ORDER BY id",
                exp_id,
            )
            return _json_resp({
                **dict(exp),
                "created_at": exp["created_at"].isoformat() if exp["created_at"] else None,
                "variants": [dict(v) for v in variants],
            })
        except Exception as exc:
            log.exception("experiment_detail uid=%d exp=%d", uid, exp_id)
            return _err(str(exc), 500)

    async def experiment_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        exp_id = int(request.match_info["exp_id"])
        await pool.execute(
            "DELETE FROM experiments e USING managed_bots b WHERE e.id=$1 AND e.bot_id=b.bot_id AND b.added_by=$2",
            exp_id, uid,
        )
        return _json_resp({"ok": True})

    # ── Health Dashboard ───────────────────────────────────────────────────────

    async def health_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            stats = await pool.fetchrow(
                """SELECT
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE acc_status='active') AS active,
                   COUNT(*) FILTER (WHERE acc_status IN ('banned','deactivated')) AS banned,
                   COUNT(*) FILTER (WHERE cooldown_until > now()) AS cooling,
                   COUNT(*) FILTER (WHERE trust_score IS NOT NULL AND trust_score < 40) AS low_trust
                   FROM tg_accounts WHERE owner_id=$1""",
                uid,
            )
            flood_7d = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log afl
                   JOIN tg_accounts ta ON ta.id=afl.account_id
                   WHERE ta.owner_id=$1 AND afl.created_at > now()-interval '7 days'""",
                uid,
            )
            warmup_active = await pool.fetchval(
                "SELECT COUNT(*) FROM account_warmup_plans WHERE owner_id=$1 AND status='active'", uid
            )
            # Recent flood events
            events = await pool.fetch(
                """SELECT afl.operation, afl.flood_seconds, afl.created_at,
                          ta.phone, ta.first_name
                   FROM account_flood_log afl
                   JOIN tg_accounts ta ON ta.id=afl.account_id
                   WHERE ta.owner_id=$1
                   ORDER BY afl.created_at DESC LIMIT 10""",
                uid,
            )
            return _json_resp({
                "total": stats["total"], "active": stats["active"],
                "banned": stats["banned"], "cooling": stats["cooling"],
                "low_trust": stats["low_trust"], "flood_7d": flood_7d,
                "warmup_active": warmup_active,
                "events": [
                    {**dict(e), "created_at": e["created_at"].isoformat() if e["created_at"] else None}
                    for e in events
                ],
            })
        except Exception as exc:
            log.exception("health_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Account Shield ─────────────────────────────────────────────────────────

    async def shield_summary(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                """SELECT
                   COUNT(*) FILTER (WHERE a.is_active = TRUE) AS total_active,
                   COUNT(*) FILTER (WHERE a.is_active = FALSE AND a.cooldown_until > NOW()) AS cooling,
                   COUNT(*) FILTER (WHERE r.risk_score >= 0.7) AS threatened,
                   COUNT(*) FILTER (WHERE r.ban_probability >= 0.5) AS high_ban,
                   COUNT(*) FILTER (WHERE r.risk_score < 0.7 AND (r.ban_probability < 0.5 OR r.ban_probability IS NULL)) AS ok_count
                   FROM tg_accounts a
                   LEFT JOIN account_risk_scores r ON r.account_id = a.id
                   WHERE a.owner_id = $1""",
                uid,
            )
            history = await pool.fetch(
                """SELECT sa.action, sa.risk_score, sa.ban_probability, sa.created_at,
                          a.phone, a.first_name
                   FROM shield_actions sa
                   JOIN tg_accounts a ON a.id = sa.account_id
                   WHERE sa.owner_id=$1 ORDER BY sa.created_at DESC LIMIT 20""",
                uid,
            )
            cfg = await pool.fetchrow(
                "SELECT * FROM shield_configs WHERE owner_id=$1", uid
            )
        except Exception as exc:
            log.exception("shield_summary uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({
            "stats": {k: int(row[k] or 0) for k in ("total_active","cooling","threatened","high_ban","ok_count")} if row else {},
            "config": {
                "risk_threshold": float(cfg["risk_threshold"]) if cfg else 0.7,
                "ban_prob_threshold": float(cfg["ban_prob_threshold"]) if cfg else 0.5,
                "auto_pause": bool(cfg["auto_pause"]) if cfg else True,
                "notify_admin": bool(cfg["notify_admin"]) if cfg else True,
            },
            "history": [
                {
                    "action": r["action"], "risk": float(r["risk_score"] or 0),
                    "ban_prob": float(r["ban_probability"] or 0),
                    "name": r["first_name"] or r["phone"] or "",
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in history
            ],
        })

    # ── Ad Intelligence ────────────────────────────────────────────────────────

    async def ad_intel_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            top = await pool.fetch(
                """SELECT channel_username, channel_title, subscribers, quality_score, er_rate, ad_price_est
                   FROM ad_placements WHERE owner_id=$1 ORDER BY quality_score DESC LIMIT 10""",
                uid,
            )
            total = await pool.fetchval("SELECT COUNT(*) FROM ad_placements WHERE owner_id=$1", uid)
            advertisers = await pool.fetch(
                """SELECT advertiser_username, placements_count, last_seen_at
                   FROM ad_advertisers WHERE owner_id=$1 ORDER BY last_seen_at DESC LIMIT 10""",
                uid,
            )
        except Exception as exc:
            log.exception("ad_intel_overview uid=%d", uid)
            return _err(str(exc), 500)
        avg_score = sum(r["quality_score"] or 0 for r in top) / max(len(top), 1)
        return _json_resp({
            "total_channels": int(total or 0),
            "avg_quality": round(avg_score, 1),
            "top_channels": [
                {
                    "username": r["channel_username"] or "",
                    "title": r["channel_title"] or "",
                    "subscribers": r["subscribers"] or 0,
                    "quality_score": round(float(r["quality_score"] or 0), 1),
                    "er_rate": round(float(r["er_rate"] or 0), 2),
                    "ad_price_est": r["ad_price_est"] or 0,
                }
                for r in top
            ],
            "top_advertisers": [
                {
                    "username": r["advertiser_username"] or "",
                    "placements": r["placements_count"] or 0,
                    "last_seen": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                }
                for r in advertisers
            ],
        })

    async def ad_intel_add_channel(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            channel = str(body.get("channel", "")).strip().lstrip("@")
        except Exception:
            return _err("bad body", 400)
        if not channel:
            return _err("channel обязателен", 400)
        import json as _json
        label = f"Ad Intel scan @{channel}"
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'ad_intel_scan','pending',$2,1,$3) RETURNING id",
            uid, _json.dumps({"channel": channel}), label,
        )
        return _json_resp({"ok": True, "op_id": op_id, "label": label})

    # ── Network / Cluster Overview ─────────────────────────────────────────────

    async def network_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bots = await pool.fetch(
                """SELECT bot_id, username, first_name, is_active, bot_role, cluster, swarm_enabled, swarm_weight
                   FROM managed_bots WHERE added_by=$1 ORDER BY cluster, bot_role""",
                uid,
            )
        except Exception as exc:
            log.exception("network_overview uid=%d", uid)
            return _err(str(exc), 500)
        # Group by cluster
        clusters: dict = {}
        for r in bots:
            cl = r["cluster"] or "default"
            if cl not in clusters:
                clusters[cl] = []
            clusters[cl].append({
                "bot_id": r["bot_id"],
                "name": r["username"] or r["first_name"] or str(r["bot_id"]),
                "is_active": bool(r["is_active"]),
                "role": r["bot_role"] or "general",
                "swarm": bool(r["swarm_enabled"]),
                "weight": float(r["swarm_weight"] or 1.0),
            })
        return _json_resp({
            "clusters": [
                {"name": name, "bots": bot_list}
                for name, bot_list in clusters.items()
            ],
            "total_bots": len(bots),
        })

    async def set_bot_role_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
            role = str(body.get("role", "general")).strip()
            cluster = str(body.get("cluster", "default")).strip()[:64]
        except Exception:
            return _err("bad request", 400)
        valid_roles = ("entry", "conversion", "retention", "general")
        if role not in valid_roles:
            return _err(f"role must be one of {valid_roles}", 400)
        res = await pool.execute(
            "UPDATE managed_bots SET bot_role=$1, cluster=$2 WHERE bot_id=$3 AND added_by=$4",
            role, cluster, bot_id, uid,
        )
        if res == "UPDATE 0":
            return _err("Not found", 404)
        return _json_resp({"ok": True})

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
                """SELECT id, op_type, COALESCE(label, op_type) AS label, status,
                          total_items, done_items, created_at, finished_at, params
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
                "processed": r["done_items"] or 0,
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

    # ── Asset Templates ──────────────────────────────────────────────────────

    async def asset_templates_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        asset_type = request.rel_url.query.get("type")
        if asset_type:
            rows = await pool.fetch(
                "SELECT id, asset_type, name, created_at FROM asset_templates "
                "WHERE owner_id=$1 AND asset_type=$2 ORDER BY created_at DESC LIMIT 50",
                uid, asset_type,
            )
        else:
            rows = await pool.fetch(
                "SELECT id, asset_type, name, created_at FROM asset_templates "
                "WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 50",
                uid,
            )
        return _json_resp([dict(r) for r in rows])

    async def asset_template_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        tpl_id = int(request.match_info["tpl_id"])
        tpl = await pool.fetchrow(
            "SELECT * FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
        )
        if not tpl:
            return _err("not found", 404)
        return _json_resp(dict(tpl))

    async def asset_template_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        tpl_id = int(request.match_info["tpl_id"])
        result = await pool.execute(
            "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
        )
        if result == "DELETE 0":
            return _err("not found", 404)
        return _json_resp({"ok": True})

    # ── Infra Health Center ───────────────────────────────────────────────────

    async def infra_health_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        alerts = await pool.fetch(
            "SELECT id, alert_type, severity, title, description, target_type, "
            "is_active, first_seen_at, resolved_at "
            "FROM infrastructure_alerts WHERE owner_id=$1 AND is_active=TRUE "
            "ORDER BY first_seen_at DESC LIMIT 20",
            uid,
        )
        recovery = await pool.fetch(
            "SELECT id, recovery_type, target_type, trigger, action, status, "
            "severity, created_at, completed_at "
            "FROM recovery_events WHERE owner_id=$1 "
            "ORDER BY created_at DESC LIMIT 20",
            uid,
        )
        return _json_resp({
            "alerts": [dict(a) for a in alerts],
            "recovery": [dict(r) for r in recovery],
        })

    # ── Swarm ─────────────────────────────────────────────────────────────────

    async def swarm_metrics(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT mb.bot_id, mb.username, mb.first_name, mb.bot_role, mb.cluster,
                   mb.swarm_weight, mb.swarm_enabled,
                   bm.ctr, bm.conversion_rate, bm.retention_d1, bm.retention_d7, bm.score,
                   bm.updated_at
            FROM managed_bots mb
            LEFT JOIN bot_metrics bm ON bm.bot_id = mb.bot_id
            WHERE mb.added_by=$1
            ORDER BY COALESCE(bm.score, 0) DESC
            LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Presence Packs ────────────────────────────────────────────────────────

    async def presence_packs_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            "SELECT id, name, description, target_url, target_label, bot_id "
            "FROM presence_packs WHERE owner_id=$1 ORDER BY id DESC LIMIT 30",
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Global Presence ───────────────────────────────────────────────────────

    async def global_presence_plans(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT gpp.id, gpp.asset_type, gpp.name_pattern, gpp.status,
                   gpp.created_at, gpp.updated_at,
                   COUNT(gpt.id) AS total_targets,
                   COUNT(gpt.id) FILTER (WHERE gpt.status='done') AS done_targets,
                   COUNT(gpt.id) FILTER (WHERE gpt.status='failed') AS failed_targets
            FROM global_presence_plans gpp
            LEFT JOIN global_presence_targets gpt ON gpt.plan_id = gpp.id
            WHERE gpp.owner_id=$1
            GROUP BY gpp.id, gpp.asset_type, gpp.name_pattern,
                     gpp.status, gpp.created_at, gpp.updated_at
            ORDER BY gpp.created_at DESC LIMIT 20
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def global_presence_plan_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        plan_id = int(request.match_info["plan_id"])
        plan = await pool.fetchrow(
            "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
        )
        if not plan:
            return _err("not found", 404)
        targets = await pool.fetch(
            "SELECT country, city, language, asset_type, planned_name, status, error_message "
            "FROM global_presence_targets WHERE plan_id=$1 ORDER BY status, country, city LIMIT 100",
            plan_id,
        )
        return _json_resp({"plan": dict(plan), "targets": [dict(t) for t in targets]})

    # ── Mass Ops ──────────────────────────────────────────────────────────────

    async def mass_ops_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT id, op_type, status, done_items, total_items, created_at, finished_at
            FROM operation_queue
            WHERE owner_id=$1 AND op_type LIKE 'mass_%'
            ORDER BY created_at DESC LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Ecosystems ────────────────────────────────────────────────────────────

    async def ecosystems_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT e.id, e.name, e.ecosystem_type, e.status, e.health_score,
                   e.risk_level, e.region, e.created_at, e.updated_at,
                   COUNT(em.id) AS member_count
            FROM ecosystems e
            LEFT JOIN ecosystem_members em ON em.ecosystem_id = e.id
            WHERE e.owner_id=$1
            GROUP BY e.id, e.name, e.ecosystem_type, e.status, e.health_score,
                     e.risk_level, e.region, e.created_at, e.updated_at
            ORDER BY e.updated_at DESC NULLS LAST
            LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def ecosystem_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        eco_id = int(request.match_info["eco_id"])
        eco = await pool.fetchrow(
            "SELECT * FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid
        )
        if not eco:
            return _err("not found", 404)
        members = await pool.fetch(
            "SELECT object_type, object_id, role, added_at "
            "FROM ecosystem_members WHERE ecosystem_id=$1 ORDER BY added_at DESC LIMIT 50",
            eco_id,
        )
        events = await pool.fetch(
            "SELECT event_type, severity, title, occurred_at "
            "FROM ecosystem_events WHERE ecosystem_id=$1 ORDER BY occurred_at DESC LIMIT 20",
            eco_id,
        )
        return _json_resp({
            "eco": dict(eco),
            "members": [dict(m) for m in members],
            "events": [dict(ev) for ev in events],
        })

    # ── Channel Factory ───────────────────────────────────────────────────────

    async def channel_factory_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        body = await request.json()
        title = (body.get("title") or "").strip()
        about = (body.get("about") or "").strip()
        account_id = body.get("account_id")
        if not title:
            return _err("title required")
        if not account_id:
            return _err("account_id required")
        acc = await pool.fetchrow(
            "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            int(account_id), uid,
        )
        if not acc:
            return _err("Аккаунт не найден или неактивен", 404)
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'create_channel','pending',$2,1,$3) RETURNING id",
            uid, json.dumps({"title": title, "about": about, "account_id": account_id}),
            f"Создать канал: {title}",
        )
        return _json_resp({"ok": True, "op_id": op_id})

    async def channel_factory_recent(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            "SELECT id, title, username, type, added_at FROM managed_channels "
            "WHERE owner_id=$1 ORDER BY added_at DESC LIMIT 20",
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Group Factory ─────────────────────────────────────────────────────────

    async def group_factory_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        body = await request.json()
        title = (body.get("title") or "").strip()
        account_id = body.get("account_id")
        is_supergroup = body.get("is_supergroup", True)
        if not title:
            return _err("title required")
        if not account_id:
            return _err("account_id required")
        acc = await pool.fetchrow(
            "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
            int(account_id), uid,
        )
        if not acc:
            return _err("Аккаунт не найден или неактивен", 404)
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'create_group','pending',$2,1,$3) RETURNING id",
            uid, json.dumps({"title": title, "account_id": account_id, "is_supergroup": is_supergroup}),
            f"Создать группу: {title}",
        )
        return _json_resp({"ok": True, "op_id": op_id})

    # ── Physics Hub ──────────────────────────────────────────────────────────

    async def physics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT ars.account_id, ta.phone, ta.username, ta.first_name,
                   ars.risk_score, ars.ban_probability, ars.flood_rate_1h,
                   ars.ops_24h, ars.last_flood_at, ars.computed_at
            FROM account_risk_scores ars
            JOIN tg_accounts ta ON ta.id = ars.account_id
            WHERE ta.owner_id = $1
            ORDER BY ars.risk_score DESC NULLS LAST
            LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def physics_account_telemetry(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        account_id = int(request.match_info["account_id"])
        owner = await pool.fetchval(
            "SELECT owner_id FROM tg_accounts WHERE id=$1", account_id
        )
        if owner != uid:
            return _err("forbidden", 403)
        rows = await pool.fetch(
            """
            SELECT op_type, outcome, COUNT(*) AS cnt,
                   AVG(flood_wait_s) AS avg_flood, AVG(duration_ms) AS avg_dur
            FROM op_telemetry
            WHERE account_id=$1 AND created_at > NOW() - INTERVAL '24 hours'
            GROUP BY op_type, outcome ORDER BY cnt DESC LIMIT 20
            """,
            account_id,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Graph Hub ─────────────────────────────────────────────────────────────

    async def graph_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        stats = await pool.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM graph_nodes) AS nodes,
                (SELECT COUNT(*) FROM graph_edges) AS edges,
                (SELECT COUNT(*) FROM audience_overlaps WHERE overlap_pct > 0.1) AS strong_overlaps
            """
        )
        return _json_resp(dict(stats) if stats else {})

    async def graph_overlaps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT ao.overlap_pct, ao.shared_users, ao.computed_at,
                   na.title AS title_a, na.username AS username_a,
                   nb.title AS title_b, nb.username AS username_b
            FROM audience_overlaps ao
            JOIN graph_nodes na ON na.id = ao.node_a
            JOIN graph_nodes nb ON nb.id = ao.node_b
            WHERE ao.overlap_pct > 0.05
            ORDER BY ao.overlap_pct DESC LIMIT 20
            """
        )
        return _json_resp([dict(r) for r in rows])

    # ── Compliance Hub ────────────────────────────────────────────────────────

    async def compliance_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        totals = await pool.fetchrow(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE outcome='success') AS ok_cnt,
                   COUNT(*) FILTER (WHERE outcome IN ('ban','flood_wait')) AS risk_cnt
            FROM compliance_audit WHERE user_id=$1
            """,
            uid,
        )
        recent = await pool.fetch(
            "SELECT op_type, outcome, created_at FROM compliance_audit "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
            uid,
        )
        return _json_resp({
            "totals": dict(totals) if totals else {},
            "recent": [dict(r) for r in recent],
        })

    # ── Content Cloner ───────────────────────────────────────────────────────

    async def content_cloner_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            "SELECT id, op_type, status, params, COALESCE(label, op_type) AS label, created_at FROM operation_queue "
            "WHERE owner_id=$1 AND op_type='content_clone' ORDER BY created_at DESC LIMIT 20",
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def content_cloner_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        body = await request.json()
        source = (body.get("source") or "").strip()
        if not source:
            return _err("source required")
        account_id = body.get("account_id")
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'content_clone','pending',$2,1,$3) RETURNING id",
            uid, json.dumps({"source": source, "account_id": account_id}),
            f"Клонировать контент: {source}",
        )
        return _json_resp({"ok": True, "op_id": op_id})

    # ── Clone Adapt ───────────────────────────────────────────────────────────

    async def clone_adapt_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT h.id, h.source_bot_id, h.target_bot_id, h.fields,
                   h.status, h.details, h.created_at,
                   sb.username AS source_uname, sb.first_name AS source_name,
                   tb.username AS target_uname, tb.first_name AS target_name
            FROM clone_adapt_history h
            LEFT JOIN managed_bots sb ON sb.bot_id = h.source_bot_id
            LEFT JOIN managed_bots tb ON tb.bot_id = h.target_bot_id
            WHERE h.owner_id=$1 ORDER BY h.created_at DESC LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Content Mesh ──────────────────────────────────────────────────────────

    async def content_meshes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT cm.id, cm.name, cm.enabled, cm.source_channel, cm.delay_minutes,
                   COUNT(DISTINCT mt.id) AS targets_count,
                   COUNT(mq.id) FILTER (WHERE mq.status='pending') AS pending_posts,
                   cm.created_at
            FROM content_meshes cm
            LEFT JOIN mesh_targets mt ON mt.mesh_id = cm.id
            LEFT JOIN mesh_queue mq ON mq.mesh_id = cm.id
            WHERE cm.owner_id=$1
            GROUP BY cm.id, cm.name, cm.enabled, cm.source_channel,
                     cm.delay_minutes, cm.created_at
            ORDER BY cm.id DESC
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def content_mesh_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        mesh_id = int(request.match_info["mesh_id"])
        mesh = await pool.fetchrow(
            "SELECT enabled FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid
        )
        if not mesh:
            return _err("not found", 404)
        new_state = not mesh["enabled"]
        await pool.execute(
            "UPDATE content_meshes SET enabled=$1, updated_at=NOW() WHERE id=$2", new_state, mesh_id
        )
        return _json_resp({"enabled": new_state})

    # ── Narrative Engine ──────────────────────────────────────────────────────

    async def narrative_campaigns_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            "SELECT id, topic, campaign_type, spread_hours, posts_total, posts_published, status, created_at "
            "FROM narrative_campaigns WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 30",
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def narrative_campaign_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        cid = int(request.match_info["campaign_id"])
        campaign = await pool.fetchrow(
            "SELECT * FROM narrative_campaigns WHERE id=$1 AND owner_id=$2", cid, uid
        )
        if not campaign:
            return _err("not found", 404)
        posts = await pool.fetch(
            "SELECT channel_username, angle, status, scheduled_at, published_at "
            "FROM narrative_posts WHERE campaign_id=$1 ORDER BY scheduled_at LIMIT 50",
            cid,
        )
        return _json_resp({"campaign": dict(campaign), "posts": [dict(p) for p in posts]})

    # ── Self Promo ───────────────────────────────────────────────────────────

    async def self_promo_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            "SELECT id, style, title, content, cta_text, cta_url, add_referral, is_active, use_count "
            "FROM self_promo_templates ORDER BY id"
        )
        return _json_resp([dict(r) for r in rows])

    async def self_promo_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        tpl_id = int(request.match_info["tpl_id"])
        tpl = await pool.fetchrow("SELECT is_active FROM self_promo_templates WHERE id=$1", tpl_id)
        if not tpl:
            return _err("not found", 404)
        new_state = not tpl["is_active"]
        await pool.execute("UPDATE self_promo_templates SET is_active=$1 WHERE id=$2", new_state, tpl_id)
        return _json_resp({"active": new_state})

    async def self_promo_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        tpl_id = int(request.match_info["tpl_id"])
        tpl = await pool.fetchrow("SELECT id, title FROM self_promo_templates WHERE id=$1 AND is_active", tpl_id)
        if not tpl:
            return _err("Шаблон не найден или неактивен", 404)
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,'self_promo_blast','pending',$2,1,$3) RETURNING id",
            uid, json.dumps({"template_id": tpl_id}),
            f"Self-promo: {tpl['title'] or tpl_id}",
        )
        return _json_resp({"ok": True, "op_id": op_id})

    # ── Semantic Memory ───────────────────────────────────────────────────────

    async def semantic_memory_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT mb.bot_id, mb.username, mb.first_name,
                   COUNT(DISTINCT bum.user_id) AS conv_users,
                   COUNT(buf.id) AS fact_count
            FROM managed_bots mb
            LEFT JOIN bot_user_memory bum ON bum.bot_id = mb.bot_id
            LEFT JOIN bot_user_facts buf ON buf.bot_id = mb.bot_id
            WHERE mb.added_by = $1
            GROUP BY mb.bot_id, mb.username, mb.first_name
            ORDER BY fact_count DESC NULLS LAST
            LIMIT 30
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def semantic_memory_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        bot_id = int(request.match_info["bot_id"])
        owner = await pool.fetchval("SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id)
        if owner != uid:
            return _err("forbidden", 403)
        facts = await pool.fetch(
            "SELECT user_id, fact_key, fact_value, confidence, updated_at "
            "FROM bot_user_facts WHERE bot_id=$1 ORDER BY updated_at DESC LIMIT 100",
            bot_id,
        )
        return _json_resp([dict(r) for r in facts])

    # ── Audience DNA ─────────────────────────────────────────────────────────

    async def audience_dna_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT ad.id, ad.bot_id, mb.username, mb.first_name,
                   ad.avg_engagement_rate, ad.churn_risk_pct,
                   ad.total_users_analyzed, ad.peak_hours, ad.peak_days,
                   ad.best_content_types, ad.top_topics, ad.computed_at
            FROM audience_dna ad
            LEFT JOIN managed_bots mb ON mb.bot_id = ad.bot_id
            WHERE ad.owner_id = $1
            ORDER BY ad.computed_at DESC
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    # ── Auto Funnels ──────────────────────────────────────────────────────────

    async def auto_funnels_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        rows = await pool.fetch(
            """
            SELECT f.id, f.name, f.bot_id, mb.username AS bot_username,
                   mb.first_name AS bot_name, f.target_segment, f.enabled, f.created_at,
                   COUNT(DISTINCT fs.id) AS steps_count,
                   COUNT(DISTINCT fr.id) FILTER (WHERE fr.status='active') AS active_runs
            FROM auto_funnels f
            LEFT JOIN managed_bots mb ON mb.bot_id = f.bot_id
            LEFT JOIN auto_funnel_steps fs ON fs.funnel_id = f.id
            LEFT JOIN auto_funnel_runs fr ON fr.funnel_id = f.id
            WHERE f.owner_id = $1
            GROUP BY f.id, f.name, f.bot_id, mb.username, mb.first_name,
                     f.target_segment, f.enabled, f.created_at
            ORDER BY f.id DESC
            """,
            uid,
        )
        return _json_resp([dict(r) for r in rows])

    async def auto_funnel_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        fid = int(request.match_info["funnel_id"])
        funnel = await pool.fetchrow(
            "SELECT enabled FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid
        )
        if not funnel:
            return _err("not found", 404)
        new_state = not funnel["enabled"]
        await pool.execute(
            "UPDATE auto_funnels SET enabled=$1, updated_at=NOW() WHERE id=$2", new_state, fid
        )
        return _json_resp({"enabled": new_state})

    async def auto_funnel_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("auth")
        fid = int(request.match_info["funnel_id"])
        funnel = await pool.fetchrow(
            """
            SELECT f.*, mb.username AS bot_username, mb.first_name AS bot_name
            FROM auto_funnels f
            LEFT JOIN managed_bots mb ON mb.bot_id = f.bot_id
            WHERE f.id=$1 AND f.owner_id=$2
            """,
            fid, uid,
        )
        if not funnel:
            return _err("not found", 404)
        steps = await pool.fetch(
            "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num", fid
        )
        return _json_resp({"funnel": dict(funnel), "steps": [dict(s) for s in steps]})

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
    # Account Shield
    app.router.add_get("/api/miniapp/shield", shield_summary)
    # Ad Intelligence
    app.router.add_get("/api/miniapp/ad_intel", ad_intel_overview)
    app.router.add_post("/api/miniapp/ad_intel/channel", ad_intel_add_channel)
    # Network / Cluster
    app.router.add_get("/api/miniapp/network", network_overview)
    app.router.add_put("/api/miniapp/bot/{bot_id}/role", set_bot_role_api)
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
    # DM Campaigns
    app.router.add_get("/api/miniapp/dm_campaigns", dm_campaigns_list)
    app.router.add_post("/api/miniapp/dm_campaigns", dm_campaign_create)
    app.router.add_post("/api/miniapp/dm_campaign/{campaign_id}/launch", dm_campaign_launch)
    app.router.add_delete("/api/miniapp/dm_campaign/{campaign_id}", dm_campaign_delete)
    # Account Warmup
    app.router.add_get("/api/miniapp/warmup", warmup_overview)
    app.router.add_post("/api/miniapp/warmup", warmup_create_plan)
    app.router.add_delete("/api/miniapp/warmup/{plan_id}", warmup_delete_plan)
    # A/B Experiments
    app.router.add_get("/api/miniapp/experiments", experiments_list)
    app.router.add_get("/api/miniapp/experiment/{exp_id}", experiment_detail)
    app.router.add_delete("/api/miniapp/experiment/{exp_id}", experiment_delete)
    # Health Dashboard
    app.router.add_get("/api/miniapp/health", health_overview)
    # Topology Map
    app.router.add_get("/api/miniapp/topology", topology_overview)
    # Infra Analytics
    app.router.add_get("/api/miniapp/infra", infra_analytics_overview)
    # Reporter
    app.router.add_post("/api/miniapp/reporter", reporter_submit)
    # Quick Post
    app.router.add_post("/api/miniapp/quick_post", quick_post_submit)
    # SEO
    app.router.add_get("/api/miniapp/seo", seo_overview)
    # Bot Factory
    app.router.add_get("/api/miniapp/bot_factory", bot_factory_status)
    # Persona Hub
    app.router.add_get("/api/miniapp/personas", persona_list)
    app.router.add_post("/api/miniapp/persona", persona_create)
    app.router.add_put("/api/miniapp/persona/{persona_id}/toggle", persona_toggle)
    app.router.add_delete("/api/miniapp/persona/{persona_id}", persona_delete)
    # Auto Registrar
    app.router.add_get("/api/miniapp/autoreg", autoreg_status)
    app.router.add_post("/api/miniapp/autoreg", autoreg_submit)
    # Phone Checker
    app.router.add_post("/api/miniapp/phone_check", phone_check_submit)
    # Referral
    app.router.add_get("/api/miniapp/referral/detail", referral_overview_detail)
    # AI Memory
    app.router.add_get("/api/miniapp/ai_memory", ai_memory_list)
    app.router.add_post("/api/miniapp/ai_memory", ai_memory_create)
    app.router.add_delete("/api/miniapp/ai_memory/{mem_id}", ai_memory_delete)
    # Nodes Hub
    app.router.add_get("/api/miniapp/nodes", nodes_list)
    app.router.add_get("/api/miniapp/node/{node_id}/threads", node_threads)
    # Gift Transfer
    app.router.add_get("/api/miniapp/gifts", gift_inventory)
    app.router.add_post("/api/miniapp/gifts/scan", gift_scan_submit)
    # Mass Inviter
    app.router.add_post("/api/miniapp/mass_invite", mass_inviter_submit)
    # Stars Hub
    app.router.add_get("/api/miniapp/stars", stars_overview)
    # Ghost Engine
    app.router.add_get("/api/miniapp/ghost", ghost_profiles)
    app.router.add_put("/api/miniapp/ghost/{profile_id}/toggle", ghost_toggle)
    app.router.add_delete("/api/miniapp/ghost/{profile_id}", ghost_delete)
    # Bot Webhook
    app.router.add_get("/api/miniapp/bot/{bot_id}/webhook", bot_webhook_info)
    app.router.add_delete("/api/miniapp/bot/{bot_id}/webhook", bot_webhook_delete)
    # Reg Checker
    app.router.add_get("/api/miniapp/reg_check/history", reg_check_history)
    app.router.add_post("/api/miniapp/reg_check", reg_check_submit)
    # Asset Templates
    app.router.add_get("/api/miniapp/asset_templates", asset_templates_list)
    app.router.add_get("/api/miniapp/asset_template/{tpl_id}", asset_template_detail)
    app.router.add_delete("/api/miniapp/asset_template/{tpl_id}", asset_template_delete)
    # Infra Health Center
    app.router.add_get("/api/miniapp/infra_health", infra_health_overview)
    # Swarm
    app.router.add_get("/api/miniapp/swarm", swarm_metrics)
    # Presence Packs
    app.router.add_get("/api/miniapp/presence_packs", presence_packs_list)
    # Global Presence
    app.router.add_get("/api/miniapp/global_presence", global_presence_plans)
    app.router.add_get("/api/miniapp/global_presence/{plan_id}", global_presence_plan_detail)
    # Mass Ops
    app.router.add_get("/api/miniapp/mass_ops", mass_ops_overview)
    # Ecosystems
    app.router.add_get("/api/miniapp/ecosystems", ecosystems_list)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}", ecosystem_detail)
    # Channel Factory
    app.router.add_post("/api/miniapp/channel_factory/submit", channel_factory_submit)
    app.router.add_get("/api/miniapp/channel_factory/recent", channel_factory_recent)
    # Group Factory
    app.router.add_post("/api/miniapp/group_factory/submit", group_factory_submit)
    # Physics Hub
    app.router.add_get("/api/miniapp/physics", physics_overview)
    app.router.add_get("/api/miniapp/physics/{account_id}/telemetry", physics_account_telemetry)
    # Graph Hub
    app.router.add_get("/api/miniapp/graph", graph_stats)
    app.router.add_get("/api/miniapp/graph/overlaps", graph_overlaps)
    # Compliance Hub
    app.router.add_get("/api/miniapp/compliance", compliance_overview)
    # Content Cloner
    app.router.add_get("/api/miniapp/content_cloner/history", content_cloner_history)
    app.router.add_post("/api/miniapp/content_cloner/submit", content_cloner_submit)
    # Clone Adapt
    app.router.add_get("/api/miniapp/clone_adapt/history", clone_adapt_history)
    # Content Mesh
    app.router.add_get("/api/miniapp/content_meshes", content_meshes_list)
    app.router.add_put("/api/miniapp/content_mesh/{mesh_id}/toggle", content_mesh_toggle)
    # Narrative Engine
    app.router.add_get("/api/miniapp/narrative", narrative_campaigns_list)
    app.router.add_get("/api/miniapp/narrative/{campaign_id}", narrative_campaign_detail)
    # Self Promo
    app.router.add_get("/api/miniapp/self_promo", self_promo_list)
    app.router.add_put("/api/miniapp/self_promo/{tpl_id}/toggle", self_promo_toggle)
    app.router.add_post("/api/miniapp/self_promo/{tpl_id}/launch", self_promo_launch)
    # Semantic Memory
    app.router.add_get("/api/miniapp/semantic_memory", semantic_memory_overview)
    app.router.add_get("/api/miniapp/semantic_memory/{bot_id}", semantic_memory_bot)
    # Audience DNA
    app.router.add_get("/api/miniapp/audience_dna", audience_dna_list)
    # Auto Funnels
    app.router.add_get("/api/miniapp/auto_funnels", auto_funnels_list)
    app.router.add_put("/api/miniapp/auto_funnel/{funnel_id}/toggle", auto_funnel_toggle)
    app.router.add_get("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_detail)
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
