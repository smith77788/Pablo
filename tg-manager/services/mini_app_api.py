"""Mini App API — aiohttp routes for the Telegram Mini App + SSE real-time updates."""
from __future__ import annotations

import asyncio
import json
import json as _json  # модульный алиас: ряд эндпоинтов используют _json без локального import
import logging
import os
from typing import Any

import asyncpg
from aiohttp import web

from services.mini_app_auth import validate_init_data, make_token, parse_token

log = logging.getLogger(__name__)


def _bot_token() -> str:
    return os.getenv("BOT_TOKEN", os.getenv("MANAGER_BOT_TOKEN", ""))


_bot_username_cache: str | None = None


async def _resolve_bot_username() -> str:
    """Реальный username системного бота. Сначала env BOT_USERNAME, иначе get_me()
    (с кэшем), чтобы фронт не подставлял хардкод вроде @botmother_bot."""
    global _bot_username_cache
    env_u = os.getenv("BOT_USERNAME", "").lstrip("@").strip()
    if env_u:
        return env_u
    if _bot_username_cache is not None:
        return _bot_username_cache
    token = _bot_token()
    if not token:
        _bot_username_cache = ""
        return ""
    try:
        from aiogram import Bot as _Bot
        _b = _Bot(token=token)
        try:
            me = await _b.get_me()
            _bot_username_cache = (me.username or "").lstrip("@")
        finally:
            try:
                await _b.session.close()
            except Exception:
                pass
    except Exception as e:
        log.warning("_resolve_bot_username failed: %s", e)
        _bot_username_cache = ""
    return _bot_username_cache


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


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def _is_admin(uid: int | None) -> bool:
    return bool(uid) and uid in _admin_ids()


def _csv_resp(filename: str, header: list[str], rows: list[list]) -> web.Response:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write("﻿")  # BOM для корректного Excel UTF-8
    w = _csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    return web.Response(
        text=buf.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Allow-Origin": "*",
        },
    )


async def _safe_count(pool: asyncpg.Pool, query: str, *args) -> int:
    try:
        return int(await pool.fetchval(query, *args) or 0)
    except Exception as e:
        log.warning("_safe_count error: %s | query=%.120s", e, query)
        return 0


async def _safe_fetch(pool: asyncpg.Pool, query: str, *args) -> list:
    try:
        rows = await pool.fetch(query, *args)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("_safe_fetch error: %s | query=%.120s", e, query)
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
        # funnels.bot_id → managed_bots.added_by = owner
        funnels_active = int(await pool.fetchval(
            """SELECT COUNT(*) FROM funnel_subscriptions fs
               JOIN funnels f ON f.id=fs.funnel_id
               JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE mb.added_by=$1 AND COALESCE(fs.completed, false)=false""", uid) or 0)
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

    async def _apply_inline_migrations(application: web.Application) -> None:
        stmts = [
            "ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS label TEXT",
            "ALTER TABLE self_promo_templates ADD COLUMN IF NOT EXISTS owner_id BIGINT",
            # tg_accounts — добавляем поля если отсутствуют
            "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS trust_score REAL",
            "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS acc_status TEXT DEFAULT 'active'",
            "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ",
            # user_proxies — полная схема
            """CREATE TABLE IF NOT EXISTS user_proxies (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                label TEXT DEFAULT '',
                proxy_url TEXT NOT NULL,
                proxy_type TEXT DEFAULT 'socks5',
                is_active BOOLEAN DEFAULT true,
                is_alive BOOLEAN DEFAULT true,
                last_check TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE(owner_id, proxy_url)
            )""",
            # auto_funnels — создаём если нет
            """CREATE TABLE IF NOT EXISTS auto_funnels (
                id SERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                bot_id BIGINT,
                name TEXT NOT NULL,
                target_segment TEXT DEFAULT 'all',
                enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )""",
            "ALTER TABLE auto_funnels ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()",
            """CREATE TABLE IF NOT EXISTS auto_funnel_steps (
                id SERIAL PRIMARY KEY,
                funnel_id INTEGER NOT NULL,
                step_num INTEGER DEFAULT 1,
                message_text TEXT,
                delay_hours INTEGER DEFAULT 0,
                completed BOOLEAN DEFAULT false
            )""",
            "ALTER TABLE auto_funnel_steps ADD COLUMN IF NOT EXISTS step_num INTEGER DEFAULT 1",
            """CREATE TABLE IF NOT EXISTS auto_funnel_runs (
                id SERIAL PRIMARY KEY,
                funnel_id INTEGER NOT NULL,
                user_id BIGINT,
                status TEXT DEFAULT 'active',
                started_at TIMESTAMPTZ DEFAULT now()
            )""",
            # platform_users — settings_json column
            "ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS settings_json TEXT",
            # account_warmup_plans — создаём если нет
            """CREATE TABLE IF NOT EXISTS account_warmup_plans (
                id             BIGSERIAL PRIMARY KEY,
                owner_id       BIGINT NOT NULL,
                account_id     BIGINT NOT NULL,
                plan_type      TEXT NOT NULL DEFAULT 'standard',
                current_day    INT  DEFAULT 0,
                target_days    INT  DEFAULT 14,
                daily_actions  INT  DEFAULT 5,
                status         TEXT DEFAULT 'active',
                started_at     TIMESTAMPTZ DEFAULT now(),
                completed_at   TIMESTAMPTZ,
                last_action_at TIMESTAMPTZ,
                meta           JSONB DEFAULT '{}'
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_warmup_account ON account_warmup_plans(account_id)",
            # v64 columns — safe to run repeatedly via ADD COLUMN IF NOT EXISTS
            "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS warmup_level FLOAT DEFAULT 0",
            "ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS last_warmup_at TIMESTAMPTZ",
            # crm_deals: fix stage CHECK constraint (schema had 'new/contacted/qualified',
            # API and JS use 'lead/contact/proposal/negotiation')
            "ALTER TABLE crm_deals ALTER COLUMN stage SET DEFAULT 'lead'",
            "ALTER TABLE crm_deals DROP CONSTRAINT IF EXISTS crm_deals_stage_check",
            "ALTER TABLE crm_deals ADD CONSTRAINT crm_deals_stage_check "
            "CHECK (stage IN ('lead','contact','proposal','negotiation','won','lost'))",
        ]
        for stmt in stmts:
            try:
                await pool.execute(stmt)
            except Exception:
                log.exception("inline migration failed: %.80s", stmt[:80])

    app.on_startup.append(_apply_inline_migrations)

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
            # План — из того же источника, что и у бота (subscriptions/get_plan),
            # иначе дашборд показывал «free» при оплаченном тарифе.
            try:
                from bot.utils.subscription import get_plan as _gp
                stats["plan"] = await _gp(pool, uid)
            except Exception:
                stats["plan"] = None
            plan_row = await pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid)
            if not stats.get("plan"):
                stats["plan"] = (plan_row["current_plan"] if plan_row else "free") or "free"
            try:
                from bot.utils.subscription import coerce_plan as _cp
                stats["plan"] = _cp(stats["plan"])
            except Exception:
                pass
            # Срок — из активной подписки, иначе из platform_users.
            _exp_row = await pool.fetchrow(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1 AND is_active=true "
                "AND expires_at > now() ORDER BY expires_at DESC LIMIT 1", uid)
            if _exp_row:
                stats["plan_expires_at"] = str(_exp_row["expires_at"])
            else:
                stats["plan_expires_at"] = str(plan_row["plan_expires_at"]) if plan_row and plan_row["plan_expires_at"] else None
        except Exception:
            stats["plan"] = "free"
            stats["plan_expires_at"] = None
        try:
            activity = await pool.fetch(
                """SELECT COALESCE(label, op_type) AS action,
                          status, created_at,
                          done_items, total_items, error_msg
                   FROM operation_queue WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 10""",
                uid)
            def _op_action(row):
                s = row["status"]
                return ("completed" if s == "done" else
                        "running" if s == "running" else
                        "error" if s == "failed" else s)
            stats["recent_activity"] = [
                {
                    "action": r["action"],
                    "status": _op_action(r),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "detail": (f'{r["done_items"]}/{r["total_items"]}' if (r["total_items"] or 0) > 0 else None),
                }
                for r in activity
            ]
        except Exception:
            stats["recent_activity"] = []
        try:
            acc_health = await pool.fetchval(
                "SELECT ROUND(AVG(COALESCE(trust_score, 100))) FROM tg_accounts WHERE owner_id=$1 AND is_active=true",
                uid)
            stats["acc_health"] = int(acc_health) if acc_health is not None else 100
        except Exception:
            stats["acc_health"] = 100
        try:
            queue_backlog = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'",
                uid)
            stats["queue_backlog"] = int(queue_backlog or 0)
        except Exception:
            stats["queue_backlog"] = 0
        try:
            ops_failed = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='failed' AND created_at > NOW() - INTERVAL '24 hours'",
                uid)
            stats["ops_failed"] = int(ops_failed or 0)
        except Exception:
            stats["ops_failed"] = 0
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
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", uid)
        return _json_resp({"bots": rows, "total": int(total or 0)})

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
        # Инлайн-кнопки (необязательно): [{text, url}] — валидируем и ограничиваем.
        buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = str(b.get("text") or "").strip()[:64]
                bu = str(b.get("url") or "").strip()
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                buttons.append({"text": bt, "url": bu})
        bot_row = await _safe_fetchrow(pool,
            "SELECT bot_id, token, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id_int, uid)
        if not bot_row:
            return _err("Bot not found", 404)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)
        try:
            try:
                row = await pool.fetchrow(
                    "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, buttons) "
                    "VALUES($1,$2,$3,'pending',$4,$5::jsonb) RETURNING id",
                    bot_id_int, text, total, uid, _json.dumps(buttons) if buttons else None)
            except Exception:
                row = await pool.fetchrow(
                    "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) VALUES($1,$2,$3,'pending',$4) RETURNING id",
                    bot_id_int, text, total, uid)
            broadcast_id = row["id"]
            # Create op_queue entry so user can track progress
            bot_label = bot_row.get("username") or bot_id_int
            label = f"Рассылка боту @{bot_label}: {text[:40]}…" if len(text) > 40 else f"Рассылка: {text[:60]}"
            _op_params = {"bot_id": bot_id_int, "broadcast_id": broadcast_id, "text": text}
            if buttons:
                _op_params["buttons"] = buttons
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps(_op_params),
                total, label,
            )
            return _json_resp({"ok": True, "broadcast_id": broadcast_id, "op_id": op_id, "total_users": total})
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
        # total — истинное число каналов (список ограничен LIMIT 50);
        # иначе подпись «N каналов» показывала бы максимум 50 вместо реального.
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid)
        return _json_resp({"channels": rows, "total": int(total or 0)})

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
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1", uid)
        return _json_resp({"campaigns": rows, "total": int(total or 0)})

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
        # Серверная агрегация KPI по ВСЕМ аккаунтам (список ограничен LIMIT 100 —
        # иначе при >100 аккаунтах счётчики считались бы по обрезанному списку).
        st = await _safe_fetchrow(pool,
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='banned') AS banned,
                      COUNT(*) FILTER (WHERE cooldown_until IS NOT NULL AND cooldown_until > now()) AS cooldown,
                      COUNT(*) FILTER (
                          WHERE is_active
                            AND COALESCE(acc_status,'ok') <> 'banned'
                            AND (cooldown_until IS NULL OR cooldown_until <= now())
                      ) AS active
               FROM tg_accounts WHERE owner_id=$1""", uid)
        stats = {k: int((st[k] if st else 0) or 0) for k in ("total", "banned", "cooldown", "active")} if st else {}
        return _json_resp({"accounts": rows, "stats": stats})

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
            except (TypeError, ValueError):
                return _err("Invalid target_id", 400)
            # IDOR-защита: бот должен принадлежать пользователю, иначе можно
            # разослать DM подписчикам чужого бота и узнать их число.
            owns_bot = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id_int, uid)
            if not owns_bot:
                return _err("Бот не найден", 404)
            total_targets = await _safe_count(pool,
                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", bot_id_int)
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
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Channel not found", 404)
        if not ch.get("acc_id"):
            return _err("No linked account for this channel", 400)
        try:
            from services.operation_bus import submit
            # Контракт _exec_bulk_post_to_channel: account_ids[], channel_ref (числовой
            # channel_id), text_to_post, bulk_access_hash. Раньше слали channel_id/
            # account_id/text — воркер их не читал и пост в канал ничего не делал.
            op_id = await submit(pool, uid, "bulk_post_to_channel", {
                "account_ids": [int(ch["acc_id"])],
                "channel_ref": int(ch_id),
                "text_to_post": text,
                "bulk_access_hash": int(ch.get("access_hash") or 0),
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
        # err_cnt — число упавших под-элементов (каналов) для показа кнопки
        # «повтор неудавшихся» даже у операций со статусом 'done' (partial-fail).
        _err_sub = ("(SELECT COUNT(*) FROM operation_log ol "
                    "WHERE ol.op_id=oq.id AND ol.status='error') AS err_cnt")
        if status_filter:
            rows = await _safe_fetch(pool,
                f"""SELECT oq.id, oq.op_type, oq.status, oq.label, oq.total_items, oq.done_items,
                          oq.error_msg, oq.created_at, oq.started_at, oq.finished_at, {_err_sub}
                   FROM operation_queue oq WHERE oq.owner_id=$1 AND oq.status=$2
                   ORDER BY oq.created_at DESC LIMIT 30""", uid, status_filter)
        else:
            rows = await _safe_fetch(pool,
                f"""SELECT oq.id, oq.op_type, oq.status, oq.label, oq.total_items, oq.done_items,
                          oq.error_msg, oq.created_at, oq.started_at, oq.finished_at, {_err_sub}
                   FROM operation_queue oq WHERE oq.owner_id=$1
                   ORDER BY oq.created_at DESC LIMIT 30""", uid)
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

    async def retry_operation(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("Invalid op_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT op_type, params, label, status FROM operation_queue WHERE id=$1 AND owner_id=$2",
                op_id, uid)
            if not row:
                return _err("Not found", 404)

            # Массовая публикация: повторяем ТОЛЬКО упавшие каналы (channel_ids из
            # operation_log). Иначе успешные каналы получили бы пост повторно
            # (дубликаты). Работает и для partial-success ('done' с ошибками) —
            # паритет с ботом (retry_failed).
            if row["op_type"] == "mass_publish":
                import json as _json
                failed = await pool.fetch(
                    "SELECT DISTINCT target FROM operation_log WHERE op_id=$1 AND status='error'",
                    op_id)
                failed_ids = [
                    int(r["target"]) for r in failed
                    if (r["target"] or "").strip().lstrip("-").isdigit()
                ]
                failed_ids = list(dict.fromkeys(failed_ids))
                if not failed_ids:
                    return _err("Нет неудавшихся каналов для повтора", 400)
                try:
                    base = row["params"] if isinstance(row["params"], dict) else _json.loads(row["params"] or "{}")
                except (TypeError, ValueError):
                    base = {}
                base = dict(base)
                base["channel_ids"] = failed_ids
                new_id = await pool.fetchval(
                    """INSERT INTO operation_queue(owner_id, op_type, params, status, label, total_items)
                       VALUES($1,$2,$3::jsonb,'pending',$4,$5) RETURNING id""",
                    uid, row["op_type"], _json.dumps(base),
                    f"Повтор неудавшихся ({len(failed_ids)})", len(failed_ids))
                return _json_resp({"ok": True, "new_id": new_id, "channels": len(failed_ids)})

            # Прочие операции — повтор целиком, только для проваленных.
            if row["status"] != "failed":
                return _err("Not found or not failed", 404)
            new_id = await pool.fetchval(
                """INSERT INTO operation_queue(owner_id, op_type, params, status, label, total_items)
                   SELECT owner_id, op_type, params, 'pending', label, total_items
                   FROM operation_queue WHERE id=$1
                   RETURNING id""",
                op_id)
            return _json_resp({"ok": True, "new_id": new_id})
        except Exception:
            log.exception("retry_operation op_id=%d uid=%d", op_id, uid)
            return _err("Failed to retry", 500)

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
        try:
            row = await pool.fetchrow(
                "SELECT note FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception as exc:
            log.exception("bot_note uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET note=$3 WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid, note or None,
            )
        except Exception as exc:
            log.exception("save_bot_note uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception as exc:
            log.exception("bot_commands uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
        if not row:
            return _err("Not found", 404)
        try:
            from services import bot_api
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                cmds = await bot_api.get_my_commands(sess, row["token"])
        except Exception as exc:
            log.exception("bot_commands get_my_commands uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception as exc:
            log.exception("set_bot_commands uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
        if not row:
            return _err("Not found", 404)
        try:
            from services import bot_api
            import aiohttp as _ahttp
            async with _ahttp.ClientSession() as sess:
                if commands:
                    ok = await bot_api.set_my_commands(sess, row["token"], commands)
                else:
                    ok = await bot_api.delete_my_commands(sess, row["token"])
        except Exception as exc:
            log.exception("set_bot_commands tg_api uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            owned = await pool.fetchval(
                "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception as exc:
            log.exception("bot_stats uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
                uid,
            )
        except Exception as exc:
            log.exception("profile_setter_status uid=%d", uid)
            return _err(str(exc), 500)
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
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW())",
                uid,
            )
        except Exception as exc:
            log.exception("profile_setter_submit fetchval uid=%d", uid)
            return _err(str(exc), 500)
        total = int(total or 0)
        use = min(acc_count, total) if acc_count > 0 else total
        if use == 0:
            return _err("Нет доступных аккаунтов", 400)
        try:
            rows = await pool.fetch(
                "SELECT id FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
                "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
                "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
                uid, use,
            )
        except Exception as exc:
            log.exception("profile_setter_submit fetch uid=%d", uid)
            return _err(str(exc), 500)
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
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'bulk_set_profile','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps(params), len(account_ids), label,
            )
        except Exception as exc:
            log.exception("profile_setter_submit insert uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "op_id": op_id, "label": label, "count": len(account_ids)})

    # ── Account Cleaner ────────────────────────────────────────────────────────

    async def cleaner_accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT id, phone, first_name,
                          (SELECT COUNT(*) FROM managed_channels WHERE acc_id=tg_accounts.id) AS asset_count
                   FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL AND session_str <> ''
                   ORDER BY added_at""",
                uid,
            )
        except Exception as exc:
            log.exception("cleaner_accounts uid=%d", uid)
            return _err(str(exc), 500)
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
        try:
            row = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND session_str IS NOT NULL",
                account_id, uid,
            )
        except Exception as exc:
            log.exception("cleaner_submit fetchrow uid=%d", uid)
            return _err(str(exc), 500)
        if not row:
            return _err("Аккаунт не найден или нет сессии", 404)
        import json as _json
        label_map = {"leave_all_chats": "Выход из чатов", "delete_contacts": "Удаление контактов"}
        label = f"Cleaner: {label_map[op]} акк #{account_id}"
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,$2,'pending',$3,1,$4) RETURNING id",
                uid, op, _json.dumps({"account_id": account_id}), label,
            )
        except Exception as exc:
            log.exception("cleaner_submit insert uid=%d", uid)
            return _err(str(exc), 500)
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
            # Channel-to-account links: managed_channels rows with an assigned account
            try:
                links = await pool.fetchval(
                    "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 AND acc_id IS NOT NULL",
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
        except Exception:
            active_accs = 0
        try:
            flood_24h = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log fl
                   JOIN tg_accounts ta ON ta.id=fl.account_id
                   WHERE ta.owner_id=$1 AND fl.created_at > NOW()-INTERVAL '24h'""",
                uid,
            )
        except Exception:
            flood_24h = 0
        try:
            ops_24h = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '24h'",
                uid,
            )
        except Exception:
            ops_24h = 0
        try:
            warmup_active = await pool.fetchval(
                """SELECT COUNT(*) FROM account_warmup_plans wp
                   WHERE wp.owner_id=$1 AND wp.status='active'""",
                uid,
            )
        except Exception:
            warmup_active = 0
        try:
            pools = await pool.fetch(
                """SELECT pool, COUNT(*) AS cnt FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND pool IS NOT NULL
                   GROUP BY pool ORDER BY cnt DESC LIMIT 10""",
                uid,
            )
        except Exception:
            pools = []
        try:
            audit_rows = await pool.fetch(
                """SELECT action, target, result, occurred_at
                   FROM operation_audit WHERE owner_id=$1
                   ORDER BY occurred_at DESC LIMIT 10""",
                uid,
            )
        except Exception:
            audit_rows = []
        return _json_resp({
            "active_accounts": int(active_accs or 0),
            "flood_24h": int(flood_24h or 0),
            "ops_24h": int(ops_24h or 0),
            "warmup_active": int(warmup_active or 0),
            "pools": [dict(p) for p in pools],
            "audit": [
                {**dict(a), "occurred_at": a["occurred_at"].isoformat() if a["occurred_at"] else None}
                for a in audit_rows
            ],
        })

    # ── Reporter (Report users) ────────────────────────────────────────────────

    async def new_users(request: web.Request) -> web.Response:
        """Лента новых подписчиков по всем ботам владельца (надёжный фид —
        не зависит от доставки push-уведомлений)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT bu.user_id, bu.username, bu.first_name, bu.first_seen,
                      mb.username AS bot_username
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0
               ORDER BY bu.first_seen DESC NULLS LAST
               LIMIT 100""", uid)
        return _json_resp({"users": rows or []})

    async def new_users_export(request: web.Request) -> web.Response:
        """Экспорт ленты новых подписчиков (CSV) по всем ботам владельца."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            """SELECT bu.user_id, bu.username, bu.first_name, bu.first_seen,
                      mb.username AS bot_username
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by = $1 AND bu.user_id > 0
               ORDER BY bu.first_seen DESC NULLS LAST
               LIMIT 10000""", uid)
        data = [[r.get("user_id"), r.get("username"), r.get("first_name"),
                 r.get("first_seen"), r.get("bot_username")] for r in (rows or [])]
        return _csv_resp("subscribers.csv",
                         ["user_id", "username", "first_name", "first_seen", "bot"], data)

    async def platform_new_users(request: web.Request) -> web.Response:
        """Лента новых пользователей системного бота @MEXAHI3MBOT (платформа).
        Только для администраторов платформы (ADMIN_IDS)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not _is_admin(uid):
            return _err("Только для администраторов платформы", 403)
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name,
                      COALESCE(current_plan,'free') AS plan,
                      COALESCE(registered_at, first_seen, last_seen) AS joined_at,
                      last_seen
               FROM platform_users
               WHERE user_id > 0
               ORDER BY COALESCE(registered_at, first_seen, last_seen) DESC NULLS LAST
               LIMIT 200""", uid)
        total = await _safe_count(pool, "SELECT COUNT(*) FROM platform_users WHERE user_id > 0")
        today = await _safe_count(pool,
            "SELECT COUNT(*) FROM platform_users WHERE COALESCE(registered_at, first_seen, last_seen) >= CURRENT_DATE")
        return _json_resp({"users": rows or [], "total": total, "today": today})

    async def platform_new_users_export(request: web.Request) -> web.Response:
        """Экспорт пользователей платформы (CSV). Только для администраторов."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not _is_admin(uid):
            return _err("Только для администраторов платформы", 403)
        rows = await _safe_fetch(pool,
            """SELECT user_id, username, first_name,
                      COALESCE(current_plan,'free') AS plan,
                      COALESCE(registered_at, first_seen, last_seen) AS joined_at,
                      last_seen
               FROM platform_users
               WHERE user_id > 0
               ORDER BY COALESCE(registered_at, first_seen, last_seen) DESC NULLS LAST
               LIMIT 50000""", uid)
        data = [[r.get("user_id"), r.get("username"), r.get("first_name"),
                 r.get("plan"), r.get("joined_at"), r.get("last_seen")] for r in (rows or [])]
        return _csv_resp("platform_users.csv",
                         ["user_id", "username", "first_name", "plan", "joined_at", "last_seen"], data)

    async def accounts_check(request: web.Request) -> web.Response:
        """Массовая проверка всех аккаунтов владельца (с реактивацией рабочих)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1", uid)
        ids = [int(r["id"]) for r in (rows or [])]
        if not ids:
            return _err("Нет аккаунтов для проверки", 400)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'check_accounts_health','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"account_ids": ids, "check_spambot": True}),
                len(ids), f"Проверка {len(ids)} аккаунтов",
            )
            return _json_resp({"ok": True, "op_id": op_id, "count": len(ids)})
        except Exception as exc:
            log.exception("accounts_check uid=%d", uid)
            return _err(str(exc), 500)

    def _build_profile_params(op: str, body: dict, ids: list) -> tuple:
        """Собрать params для profile_setter. Возвращает (params, label) или (None, error)."""
        params: dict = {"op": op, "account_ids": ids}
        if op == "name":
            fn = (body.get("first_name") or "").strip()
            if not fn:
                return None, "Укажите имя"
            params["name_data"] = {
                "first_name": fn,
                "last_name": (body.get("last_name") or "").strip(),
                "about": (body.get("about") or "").strip(),
            }
            return params, "Смена имени/bio"
        if op == "avatar":
            url = (body.get("avatar_url") or "").strip()
            if not url:
                return None, "Укажите ссылку на аватар"
            params["avatar_url"] = url
            return params, "Смена аватара"
        if op == "2fa":
            np = (body.get("new_password") or "").strip()
            if not np:
                return None, "Укажите новый пароль"
            params["new_password"] = np
            params["current_password"] = (body.get("current_password") or "").strip()
            params["hint"] = (body.get("hint") or "").strip()
            return params, "Смена 2FA"
        return None, "Неизвестная операция"

    async def accounts_mass(request: web.Request) -> web.Response:
        """Массовое действие над выбранными аккаунтами.
        body: {op: check|scan|leave_all|name|avatar|2fa, account_ids: [..], ...}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        op = body.get("op")
        ids_in = [int(x) for x in (body.get("account_ids") or []) if str(x).lstrip("-").isdigit()]
        if not ids_in:
            return _err("Выберите аккаунты", 400)
        owned = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
            uid, ids_in)
        ids = [int(r["id"]) for r in (owned or [])]
        if not ids:
            return _err("Аккаунты не найдены", 404)
        n = len(ids)
        try:
            if op == "check":
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'check_accounts_health','pending',$2,$3,$4) RETURNING id",
                    uid, _json.dumps({"account_ids": ids, "check_spambot": True}), n,
                    f"Проверка {n} аккаунтов")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "scan":
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'scan_owned_resources','pending',$2,$3,$4) RETURNING id",
                    uid, _json.dumps({"account_ids": ids}), n,
                    f"Скан ресурсов: {n} акк.")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "leave_all":
                # leave_all_chats — по одному аккаунту, ставим N операций
                op_ids = []
                for aid in ids:
                    oid = await pool.fetchval(
                        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                        "VALUES($1,'leave_all_chats','pending',$2,1,$3) RETURNING id",
                        uid, _json.dumps({"account_id": aid}), f"Выход из всех чатов (акк. {aid})")
                    op_ids.append(int(oid))
                return _json_resp({"ok": True, "op_ids": op_ids, "count": n})
            if op in ("name", "avatar", "2fa"):
                params, label = _build_profile_params(op, body, ids)
                if params is None:
                    return _err(label, 400)
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'profile_setter','pending',$2,$3,$4) RETURNING id",
                    uid, _json.dumps(params), n, f"{label}: {n} акк.")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            return _err("Неизвестная операция", 400)
        except Exception as exc:
            log.exception("accounts_mass uid=%d op=%s", uid, op)
            return _err(str(exc), 500)

    async def account_profile(request: web.Request) -> web.Response:
        """Сменить профиль аккаунта: имя/bio | аватар | 2FA (op).
        Маппится на op_type=profile_setter (контракт _exec_bulk_set_profile)."""
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
            return _err("Аккаунт не найден", 404)
        op = body.get("op")
        params: dict = {"op": op, "account_ids": [acc_id]}
        if op == "name":
            fn = (body.get("first_name") or "").strip()
            if not fn:
                return _err("Укажите имя", 400)
            params["name_data"] = {
                "first_name": fn,
                "last_name": (body.get("last_name") or "").strip(),
                "about": (body.get("about") or "").strip(),
            }
            label = "Смена имени/bio"
        elif op == "avatar":
            url = (body.get("avatar_url") or "").strip()
            if not url:
                return _err("Укажите ссылку на аватар", 400)
            params["avatar_url"] = url
            label = "Смена аватара"
        elif op == "2fa":
            np = (body.get("new_password") or "").strip()
            if not np:
                return _err("Укажите новый пароль", 400)
            params["new_password"] = np
            params["current_password"] = (body.get("current_password") or "").strip()
            params["hint"] = (body.get("hint") or "").strip()
            label = "Смена 2FA"
        else:
            return _err("Неизвестная операция", 400)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'profile_setter','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps(params), label)
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("account_profile uid=%d acc=%d op=%s", uid, acc_id, op)
            return _err(str(exc), 500)

    async def channel_edit(request: web.Request) -> web.Response:
        """Изменить описание/username канала (op: about|username).
        Маппится на op_type=bulk_chan_exec (per-channel пара)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        op = body.get("op")
        value = (body.get("value") or "").strip()
        if op not in ("about", "username") or not value:
            return _err("Укажите op (about|username) и значение", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        worker_op = "chan_about" if op == "about" else "chan_uname"
        params = {
            "op": worker_op,
            "value": value,
            "base_uname": value,
            "channel_acc_pairs": [{"channel_id": ch_id, "acc_id": int(ch["acc_id"]), "title": ch.get("title") or ""}],
        }
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'bulk_chan_exec','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps(params), f"Канал: {op}")
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("channel_edit uid=%d ch=%d op=%s", uid, ch_id, op)
            return _err(str(exc), 500)

    async def channel_promote(request: web.Request) -> web.Response:
        """Назначить все аккаунты администраторами канала (promote_all_admins)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("bad ch_id", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта (создателя)", 400)
        params = {"channel_id": ch_id, "owner_acc_id": int(ch["acc_id"])}
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'promote_all_admins','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps(params), "Назначение админов")
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("channel_promote uid=%d ch=%d", uid, ch_id)
            return _err(str(exc), 500)

    async def channels_mass(request: web.Request) -> web.Response:
        """Массовое действие над выбранными каналами.
        body: {op: post|about|username|promote, channel_ids: [..], value?/text?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        op = body.get("op")
        ids_in = [int(x) for x in (body.get("channel_ids") or []) if str(x).lstrip("-").isdigit()]
        if not ids_in:
            return _err("Выберите каналы", 400)
        chans = await _safe_fetch(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels "
            "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])", uid, ids_in)
        chans = [c for c in (chans or []) if c.get("acc_id")]
        if not chans:
            return _err("Каналы не найдены или нет привязанного аккаунта", 404)
        n = len(chans)
        try:
            if op in ("about", "username"):
                value = (body.get("value") or "").strip()
                if not value:
                    return _err("Укажите значение", 400)
                worker_op = "chan_about" if op == "about" else "chan_uname"
                pairs = [{"channel_id": int(c["channel_id"]), "acc_id": int(c["acc_id"]),
                          "title": c.get("title") or ""} for c in chans]
                params = {"op": worker_op, "value": value, "base_uname": value,
                          "channel_acc_pairs": pairs}
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'bulk_chan_exec','pending',$2,$3,$4) RETURNING id",
                    uid, _json.dumps(params), n, f"Каналы ({op}): {n}")
                return _json_resp({"ok": True, "op_id": op_id, "count": n})
            if op == "post":
                text = (body.get("text") or "").strip()
                if not text:
                    return _err("Введите текст поста", 400)
                if len(text) > 4096:
                    return _err("Слишком длинный текст (макс. 4096)", 400)
                from services.operation_bus import submit
                op_ids = []
                for c in chans:
                    oid = await submit(pool, uid, "bulk_post_to_channel", {
                        "account_ids": [int(c["acc_id"])],
                        "channel_ref": int(c["channel_id"]),
                        "text_to_post": text,
                        "bulk_access_hash": int(c.get("access_hash") or 0),
                    }, total_items=1)
                    op_ids.append(int(oid))
                return _json_resp({"ok": True, "op_ids": op_ids, "count": n})
            if op == "promote":
                op_ids = []
                for c in chans:
                    oid = await pool.fetchval(
                        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                        "VALUES($1,'promote_all_admins','pending',$2,1,$3) RETURNING id",
                        uid, _json.dumps({"channel_id": int(c["channel_id"]),
                                          "owner_acc_id": int(c["acc_id"])}),
                        f"Админы: {c.get('title') or c['channel_id']}")
                    op_ids.append(int(oid))
                return _json_resp({"ok": True, "op_ids": op_ids, "count": n})
            return _err("Неизвестная операция", 400)
        except Exception as exc:
            log.exception("channels_mass uid=%d op=%s", uid, op)
            return _err(str(exc), 500)

    async def channel_remove(request: web.Request) -> web.Response:
        """Убрать канал из управления (запись managed_channels)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("bad ch_id", 400)
        try:
            res = await pool.execute(
                "DELETE FROM managed_channels WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
            if str(res).endswith(" 0"):
                return _err("Канал не найден", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("channel_remove uid=%d ch=%d", uid, ch_id)
            return _err(str(exc), 500)

    async def account_toggle(request: web.Request) -> web.Response:
        """Вкл/выкл аккаунта (is_active)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT is_active FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if not row:
                return _err("Аккаунт не найден", 404)
            new_state = not bool(row["is_active"])
            await pool.execute(
                "UPDATE tg_accounts SET is_active=$1 WHERE id=$2 AND owner_id=$3",
                new_state, acc_id, uid)
            return _json_resp({"ok": True, "is_active": new_state})
        except Exception as exc:
            log.exception("account_toggle uid=%d acc=%d", uid, acc_id)
            return _err(str(exc), 500)

    async def account_delete(request: web.Request) -> web.Response:
        """Удалить аккаунт."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        try:
            res = await pool.execute(
                "DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("account_delete uid=%d acc=%d", uid, acc_id)
            return _err(str(exc), 500)

    async def account_action(request: web.Request) -> web.Response:
        """Операция от имени одного аккаунта: scan | leave_all.
        Маппится на существующие op_type (scan_owned_resources / leave_all_chats)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            act = request.match_info["act"]
        except (KeyError, ValueError):
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        if act == "scan":
            op_type, params, label = "scan_owned_resources", {"account_ids": [acc_id]}, "Скан ресурсов аккаунта"
        elif act == "leave_all":
            op_type, params, label = "leave_all_chats", {"account_id": acc_id}, "Выход из всех чатов"
        else:
            return _err("Неизвестное действие", 400)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,$2,'pending',$3,1,$4) RETURNING id",
                uid, op_type, _json.dumps(params), label)
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("account_action uid=%d acc=%d act=%s", uid, acc_id, act)
            return _err(str(exc), 500)

    async def account_check_one(request: web.Request) -> web.Response:
        """Проверить один аккаунт (с реактивацией если рабочий)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'check_accounts_health','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"account_ids": [acc_id], "check_spambot": True}),
                "Проверка аккаунта")
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("account_check_one uid=%d acc=%d", uid, acc_id)
            return _err(str(exc), 500)

    async def diag(request: web.Request) -> web.Response:
        """Сквозная диагностика исполнения: креды/транспорт, аккаунты, очередь,
        живой тест подключения одного аккаунта (тот же путь, что у операций)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        import time as _t
        try:
            from config import TG_API_ID, CF_RELAY_URL, TG_PROXY
        except Exception:
            TG_API_ID, CF_RELAY_URL, TG_PROXY = 0, "", ""
        report: dict = {
            "env": {
                "api_configured": bool(TG_API_ID),
                "cf_relay": bool(CF_RELAY_URL),
                "tg_proxy": bool(TG_PROXY),
            },
            "accounts": {}, "queue": {}, "live_test": {},
        }
        try:
            a = await pool.fetchrow(
                "SELECT COUNT(*) AS total, "
                "COUNT(*) FILTER (WHERE is_active) AS active, "
                "COUNT(*) FILTER (WHERE session_str IS NOT NULL AND session_str<>'') AS with_session, "
                "COUNT(*) FILTER (WHERE proxy_id IS NOT NULL) AS with_proxy "
                "FROM tg_accounts WHERE owner_id=$1", uid)
            report["accounts"] = {k: int(v or 0) for k, v in dict(a).items()} if a else {}
        except Exception as e:
            report["accounts"] = {"error": str(e)[:120]}
        try:
            q = await pool.fetchrow(
                "SELECT COUNT(*) FILTER (WHERE status='pending') AS pending, "
                "COUNT(*) FILTER (WHERE status='running') AS running, "
                "COUNT(*) FILTER (WHERE status='done' AND finished_at>now()-interval '24 hours') AS done_24h, "
                "COUNT(*) FILTER (WHERE status='failed' AND finished_at>now()-interval '24 hours') AS failed_24h "
                "FROM operation_queue WHERE owner_id=$1", uid)
            report["queue"] = {k: int(v or 0) for k, v in dict(q).items()} if q else {}
        except Exception as e:
            report["queue"] = {"error": str(e)[:120]}
        try:
            row = await pool.fetchrow(
                "SELECT a.id, a.session_str, a.first_name, a.phone, a.device_model, a.system_version, "
                "a.app_version, a.lang_code, a.system_lang_code, p.proxy_url "
                "FROM tg_accounts a LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
                "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL "
                "ORDER BY a.trust_score DESC NULLS LAST LIMIT 1", uid)
            if not row:
                report["live_test"] = {"ok": False, "reason": "Нет активного аккаунта с сессией для теста"}
            else:
                from services.account_manager import check_account_status_full
                t0 = _t.monotonic()
                res = await check_account_status_full(
                    row["session_str"], _acc=dict(row), check_spambot=False)
                report["live_test"] = {
                    "ok": res.get("status") == "active",
                    "account": str(row["first_name"] or row["phone"] or row["id"]),
                    "status": res.get("status"),
                    "reason": (res.get("reason") or "")[:200],
                    "latency_ms": round((_t.monotonic() - t0) * 1000),
                    "via_proxy": bool(row["proxy_url"]),
                }
        except Exception as e:
            report["live_test"] = {"ok": False, "reason": f"Ошибка теста подключения: {str(e)[:160]}"}
        return _json_resp(report)

    async def boost_submit(request: web.Request) -> web.Response:
        """Накрутка: просмотры / реакции / сторис через аккаунты владельца.
        body: {type: views|reactions|stories, channel|target, msg_ids|msg_id, emoji, acc_count}
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        btype = (body.get("type") or "").strip()
        if btype not in ("views", "reactions", "stories"):
            return _err("Неверный тип накрутки", 400)
        try:
            acc_count = int(body.get("acc_count") or 0)
        except (TypeError, ValueError):
            acc_count = 0

        # Подбор аккаунтов как в боте: активные, без кулдауна, по trust_score.
        rows = await _safe_fetch(pool,
            "SELECT id FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
            "AND (cooldown_until IS NULL OR cooldown_until < NOW()) "
            "ORDER BY trust_score DESC NULLS LAST LIMIT $2",
            uid, acc_count if acc_count > 0 else 1000)
        account_ids = [r["id"] for r in (rows or [])]
        if not account_ids:
            return _err("Нет доступных аккаунтов", 400)

        def _parse_ids(raw: str) -> list[int]:
            out: list[int] = []
            for part in str(raw or "").replace(" ", "").split(","):
                if not part:
                    continue
                if "-" in part:
                    try:
                        a, b = part.split("-", 1)
                        a, b = int(a), int(b)
                        if 0 < b - a <= 1000:
                            out.extend(range(a, b + 1))
                    except ValueError:
                        continue
                else:
                    try:
                        out.append(int(part))
                    except ValueError:
                        continue
            # dedup, preserve order
            seen: set[int] = set()
            return [x for x in out if not (x in seen or seen.add(x))]

        if btype == "views":
            channel = (body.get("channel") or "").strip()
            msg_ids = _parse_ids(body.get("msg_ids"))
            if not channel or not msg_ids:
                return _err("Укажите канал и ID сообщений", 400)
            op_type = "boost_views"
            params = {"channel": channel, "msg_ids": msg_ids, "account_ids": account_ids}
            total = len(account_ids) * len(msg_ids)
            label = f"Просмотры: {channel} × {len(msg_ids)} × {len(account_ids)} акк."
        elif btype == "reactions":
            channel = (body.get("channel") or "").strip()
            try:
                msg_id = int(body.get("msg_id") or 0)
            except (TypeError, ValueError):
                msg_id = 0
            emoji = (body.get("emoji") or "👍").strip() or "👍"
            if not channel or not msg_id:
                return _err("Укажите канал и ID сообщения", 400)
            op_type = "boost_reactions"
            params = {"channel": channel, "msg_id": msg_id, "emoji": emoji, "account_ids": account_ids}
            total = len(account_ids)
            label = f"Реакции {emoji}: {channel} × {len(account_ids)} акк."
        else:  # stories
            target = (body.get("target") or "").strip()
            if not target:
                return _err("Укажите цель (@username)", 400)
            op_type = "boost_stories"
            params = {"target": target, "account_ids": account_ids}
            total = len(account_ids)
            label = f"Сторис: {target} × {len(account_ids)} акк."

        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,$2,'pending',$3,$4,$5) RETURNING id",
                uid, op_type, _json.dumps(params), total, label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "accounts": len(account_ids)})
        except Exception as exc:
            log.exception("boost_submit uid=%d type=%s", uid, btype)
            return _err(str(exc), 500)

    async def growth_submit(request: web.Request) -> web.Response:
        """Growth Agent: постинг промо-текста в нишевых группах.
        body: {niche, promo_text}
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        niche = (body.get("niche") or "").strip()
        promo_text = (body.get("promo_text") or "").strip()
        if not niche:
            return _err("Укажите нишу", 400)
        if not promo_text:
            return _err("Укажите рекламный текст", 400)
        # Нужен хотя бы один активный аккаунт с сессией.
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"Growth Agent: {niche[:40]}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'niche_growth_post','pending',$2,5,$3) RETURNING id",
                uid, _json.dumps({"niche": niche, "promo_text": promo_text}), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("growth_submit uid=%d", uid)
            return _err(str(exc), 500)

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
                   LEFT JOIN managed_channels c ON c.channel_id=s.chan_id
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

    async def bot_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        token = str(data.get("token", "")).strip()
        if not token:
            return _err("token обязателен", 400)
        import re as _re
        if not _re.match(r'^\d+:[A-Za-z0-9_-]{30,}$', token):
            return _err("Неверный формат токена. Пример: 1234567890:AAHxxxxxxxx...", 400)
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _http:
                async with _http.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=_aio.ClientTimeout(total=10),
                ) as _resp:
                    me = await _resp.json()
            if not me.get("ok"):
                desc = me.get("description") or "неверный токен"
                return _err(f"Telegram API: {desc}", 400)
            bot_info = me["result"]
            bot_id = bot_info["id"]
            username = bot_info.get("username", "")
            first_name = bot_info.get("first_name", "")
            from database import db as _db
            # Лимит тарифа: enforce только для нового бота (повторное добавление
            # уже своего бота идемпотентно и не должно блокироваться).
            already_owned = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
            if not already_owned:
                from bot.utils.subscription import get_bot_limit, get_effective_bot_count
                _lim = await get_bot_limit(pool, uid)
                if await get_effective_bot_count(pool, uid) >= _lim:
                    return _err(f"Достигнут лимит ботов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            result = await _db.add_bot(pool, token, bot_id, username, first_name, uid)
            if result == "taken":
                return _err("Этот бот уже добавлен другим пользователем", 409)
            already = (result is False)
            if already:
                return _json_resp({
                    "ok": True, "already_exists": True,
                    "bot_id": bot_id, "username": username, "first_name": first_name,
                })
            return _json_resp({
                "ok": True, "already_exists": False,
                "bot_id": bot_id, "username": username, "first_name": first_name,
            })
        except Exception as exc:
            log.exception("bot_add uid=%d", uid)
            return _err(str(exc), 500)

    async def bot_remove(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
            if not row:
                return _err("Бот не найден", 404)
            await pool.execute(
                "UPDATE managed_bots SET is_active=FALSE WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("bot_remove uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    # ── Persona Hub ───────────────────────────────────────────────────────────

    async def persona_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT pp.id, pp.persona_name, pp.bio, pp.age, pp.speech_style,
                          pp.tone, pp.niche, pp.is_active, pp.created_at, pp.interests,
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
        try:
            persona_id = int(request.match_info["persona_id"])
        except (KeyError, ValueError):
            return _err("bad persona_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, is_active FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_val = not row["is_active"]
            await pool.execute(
                "UPDATE persona_profiles SET is_active=$1 WHERE id=$2 AND owner_id=$3", new_val, persona_id, uid
            )
            return _json_resp({"is_active": new_val})
        except Exception as exc:
            log.exception("persona_toggle uid=%d persona=%d", uid, persona_id)
            return _err(str(exc), 500)

    async def persona_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            persona_id = int(request.match_info["persona_id"])
        except (KeyError, ValueError):
            return _err("bad persona_id", 400)
        try:
            await pool.execute(
                "DELETE FROM persona_profiles WHERE id=$1 AND owner_id=$2", persona_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("persona_delete uid=%d persona=%d", uid, persona_id)
            return _err(str(exc), 500)

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
        # Auto-registration requires interactive SMS verification and must be done
        # through the bot (/reg command). The Mini App cannot handle this flow.
        return _err(
            "Авторегистрация выполняется только через бота: /reg\n"
            "Введите /reg в диалоге с ботом и следуйте инструкциям.",
            400,
        )

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
        try:
            mem_id = int(request.match_info["mem_id"])
        except (KeyError, ValueError):
            return _err("bad mem_id", 400)
        try:
            await pool.execute(
                "DELETE FROM botmother_memory WHERE id=$1 AND owner_id=$2", mem_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("ai_memory_delete uid=%d mem=%d", uid, mem_id)
            return _err(str(exc), 500)

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
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("bad node_id", 400)
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

    async def node_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        tg_chat_id_raw = data.get("tg_chat_id")
        node_type = str(data.get("node_type", "workspace")).strip()
        name = str(data.get("name", "")).strip()
        if not tg_chat_id_raw or not name:
            return _err("tg_chat_id и name обязательны", 400)
        if node_type not in ("proxies", "accounts", "tasks", "alerts", "workspace"):
            node_type = "workspace"
        try:
            tg_chat_id = int(str(tg_chat_id_raw).replace("-100", "-100").strip())
        except (ValueError, TypeError):
            return _err("tg_chat_id должен быть числом (например -1001234567890)", 400)
        try:
            nid = await pool.fetchval(
                """INSERT INTO bm_telegram_nodes (owner_id, tg_chat_id, node_type, name)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (owner_id, tg_chat_id, node_type) DO UPDATE
                     SET name=EXCLUDED.name, is_active=TRUE
                   RETURNING id""",
                uid, tg_chat_id, node_type, name,
            )
            return _json_resp({"id": nid, "ok": True})
        except Exception as exc:
            log.exception("node_create uid=%d", uid)
            return _err(str(exc), 500)

    async def node_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("bad node_id", 400)
        try:
            await pool.execute(
                "DELETE FROM bm_telegram_nodes WHERE id=$1 AND owner_id=$2", node_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("node_delete uid=%d node=%d", uid, node_id)
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
        account_ids = body.get("account_ids") or []
        if account_ids:
            req_ids = [int(x) for x in account_ids if str(x).isdigit()]
            # Фильтруем по владельцу (как в accounts_mass) — не полагаемся только
            # на повторный скоуп в executor.
            owned = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
                uid, req_ids)
            account_ids = [int(r["id"]) for r in (owned or [])]
            if not account_ids:
                return _err("Аккаунты не найдены", 404)
        try:
            label = f"Mass Invite → {group}"
            params = {"group": group, "source": source}
            if account_ids:
                params["account_ids"] = account_ids
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'mass_invite','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps(params), label,
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

    async def stars_experiment_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        bot_id = data.get("bot_id")
        name = str(data.get("name", "")).strip()
        content_type = data.get("content_type", "message")
        price_a = int(data.get("price_a", 50))
        price_b = int(data.get("price_b", 100))
        if not bot_id or not name:
            return _err("bot_id и name обязательны", 400)
        if content_type not in ("message", "media", "subscription", "gift"):
            content_type = "message"
        if price_a < 1 or price_b < 1:
            return _err("Цены должны быть > 0", 400)
        try:
            bot = await pool.fetchrow(
                "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", int(bot_id), uid
            )
            if not bot:
                return _err("Бот не найден", 404)
            eid = await pool.fetchval(
                """INSERT INTO stars_experiments
                   (bot_id, owner_id, name, content_type, price_a, price_b, status)
                   VALUES ($1, $2, $3, $4, $5, $6, 'active') RETURNING id""",
                int(bot_id), uid, name, content_type, price_a, price_b,
            )
            return _json_resp({"id": eid, "ok": True})
        except Exception as exc:
            log.exception("stars_experiment_create uid=%d", uid)
            return _err(str(exc), 500)

    async def stars_experiment_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eid = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("bad exp_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, status FROM stars_experiments WHERE id=$1 AND owner_id=$2", eid, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_status = "paused" if row["status"] == "active" else "active"
            await pool.execute(
                "UPDATE stars_experiments SET status=$1 WHERE id=$2 AND owner_id=$3", new_status, eid, uid
            )
            return _json_resp({"status": new_status})
        except Exception as exc:
            log.exception("stars_experiment_toggle uid=%d exp=%d", uid, eid)
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
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("bad profile_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, enabled FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
            )
            if not row:
                return _err("Не найдено", 404)
            new_val = not row["enabled"]
            await pool.execute(
                "UPDATE ghost_profiles SET enabled=$1, updated_at=now() WHERE id=$2 AND owner_id=$3", new_val, profile_id, uid
            )
            return _json_resp({"enabled": new_val})
        except Exception as exc:
            log.exception("ghost_toggle uid=%d profile=%d", uid, profile_id)
            return _err(str(exc), 500)

    async def ghost_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            profile_id = int(request.match_info["profile_id"])
        except (KeyError, ValueError):
            return _err("bad profile_id", 400)
        try:
            await pool.execute(
                "DELETE FROM ghost_profiles WHERE id=$1 AND owner_id=$2", profile_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("ghost_delete uid=%d profile=%d", uid, profile_id)
            return _err(str(exc), 500)

    async def ghost_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        account_id = data.get("account_id")
        personality = data.get("personality", "ghost")
        active_hours_start = int(data.get("active_hours_start", 9))
        active_hours_end = int(data.get("active_hours_end", 23))
        daily_cap = int(data.get("daily_cap", 8))
        cooldown_minutes = int(data.get("cooldown_minutes", 60))
        if not account_id:
            return _err("account_id обязателен", 400)
        if personality not in ("ghost", "watcher", "active"):
            return _err("personality: ghost|watcher|active", 400)
        try:
            acc = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2", int(account_id), uid
            )
            if not acc:
                return _err("Аккаунт не найден", 404)
            pid = await pool.fetchval(
                """INSERT INTO ghost_profiles
                   (owner_id, account_id, personality, active_hours_start, active_hours_end,
                    daily_cap, cooldown_minutes)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (owner_id, account_id) DO UPDATE
                     SET personality=EXCLUDED.personality,
                         active_hours_start=EXCLUDED.active_hours_start,
                         active_hours_end=EXCLUDED.active_hours_end,
                         daily_cap=EXCLUDED.daily_cap,
                         cooldown_minutes=EXCLUDED.cooldown_minutes,
                         updated_at=now()
                   RETURNING id""",
                uid, int(account_id), personality,
                active_hours_start, active_hours_end, daily_cap, cooldown_minutes,
            )
            return _json_resp({"id": pid, "ok": True})
        except Exception as exc:
            log.exception("ghost_create uid=%d", uid)
            return _err(str(exc), 500)

    # ── Bot Webhook ────────────────────────────────────────────────────────────

    async def bot_webhook_info(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                bot_id, uid,
            )
        except Exception as exc:
            log.exception("bot_webhook_info uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception as exc:
            log.exception("bot_webhook_delete uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
            total = await _safe_count(pool,
                "SELECT COUNT(*) FROM dm_campaigns WHERE owner_id=$1", uid)
            return _json_resp({"campaigns": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ], "total": int(total or 0)})
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
        # IDOR-защита: при таргете на конкретного бота проверяем владение.
        if target_type in ("bot_users", "cohort") and target_id:
            try:
                _bid = int(target_id)
            except (TypeError, ValueError):
                return _err("Invalid target_id", 400)
            owns_bot = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", _bid, uid)
            if not owns_bot:
                return _err("Бот не найден", 404)
        # Calculate total_targets depending on type
        total_targets = 0
        try:
            if target_type == "bot_users" and target_id:
                total_targets = await _safe_count(pool,
                    "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true", int(target_id))
            elif target_type == "all_bots":
                total_targets = await _safe_count(pool,
                    """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                       JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                       WHERE mb.added_by=$1 AND bu.is_active=true""", uid)
        except Exception:
            total_targets = 0
        try:
            row = await pool.fetchrow(
                """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, total_targets)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, name, text, target_type, int(target_id) if target_id else None, total_targets,
            )
            return _json_resp({"id": row["id"], "total_targets": total_targets})
        except Exception as exc:
            log.exception("dm_campaign_create uid=%d", uid)
            return _err(str(exc), 500)

    async def dm_campaign_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("bad campaign_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT id, name, status FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
                campaign_id, uid,
            )
        except Exception as exc:
            log.exception("dm_campaign_launch fetch uid=%d", uid)
            return _err(str(exc), 500)
        if not row:
            return _err("Не найдено", 404)
        if row["status"] == "running":
            return _err("Кампания уже выполняется", 409)
        try:
            label = f"DM-кампания: {row['name']}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'dm_campaign','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"campaign_id": campaign_id}), label,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET status='running', started_at=now() WHERE id=$1 AND owner_id=$2", campaign_id, uid
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("dm_campaign_launch uid=%d cid=%d", uid, campaign_id)
            return _err(str(exc), 500)

    async def dm_campaign_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            campaign_id = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("bad campaign_id", 400)
        try:
            await pool.execute(
                "DELETE FROM dm_campaigns WHERE id=$1 AND owner_id=$2", campaign_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("dm_campaign_delete uid=%d cid=%d", uid, campaign_id)
            return _err(str(exc), 500)

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
        try:
            acc = await pool.fetchrow(
                "SELECT id, session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2", account_id, uid
            )
        except Exception as exc:
            log.exception("warmup_create_plan fetchrow uid=%d", uid)
            return _err(str(exc), 500)
        if not acc:
            return _err("Аккаунт не найден", 404)
        if not acc["session_str"]:
            return _err("Аккаунт не имеет активной сессии — сначала добавьте .session файл", 400)
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
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("bad plan_id", 400)
        try:
            await pool.execute(
                "DELETE FROM account_warmup_plans WHERE id=$1 AND owner_id=$2", plan_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("warmup_delete_plan uid=%d plan=%d", uid, plan_id)
            return _err(str(exc), 500)

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
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("bad exp_id", 400)
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
        try:
            exp_id = int(request.match_info["exp_id"])
        except (KeyError, ValueError):
            return _err("bad exp_id", 400)
        try:
            await pool.execute(
                "DELETE FROM experiments e USING managed_bots b WHERE e.id=$1 AND e.bot_id=b.bot_id AND b.added_by=$2",
                exp_id, uid,
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("experiment_delete uid=%d exp=%d", uid, exp_id)
            return _err(str(exc), 500)

    async def experiment_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        bot_id = data.get("bot_id")
        name = str(data.get("name", "")).strip()
        experiment_type = data.get("experiment_type", "start_message")
        variants = data.get("variants", [])
        if not bot_id or not name:
            return _err("bot_id и name обязательны", 400)
        if experiment_type not in ("start_message", "auto_reply", "funnel"):
            experiment_type = "start_message"
        if not variants or len(variants) < 2:
            return _err("Нужно минимум 2 варианта", 400)
        try:
            bot = await pool.fetchrow(
                "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2", int(bot_id), uid
            )
            if not bot:
                return _err("Бот не найден", 404)
            async with pool.acquire() as conn:
                async with conn.transaction():
                    exp_id = await conn.fetchval(
                        "INSERT INTO experiments (bot_id, name, experiment_type) VALUES ($1,$2,$3) RETURNING id",
                        int(bot_id), name, experiment_type,
                    )
                    for v in variants[:4]:
                        await conn.execute(
                            "INSERT INTO experiment_variants (experiment_id, name, content, weight) VALUES ($1,$2,$3,$4)",
                            exp_id,
                            str(v.get("name", "Вариант"))[:100],
                            str(v.get("content", ""))[:4000],
                            min(100, max(1, int(v.get("weight", 50)))),
                        )
            return _json_resp({"id": exp_id, "ok": True})
        except Exception as exc:
            log.exception("experiment_create uid=%d", uid)
            return _err(str(exc), 500)

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
                   COUNT(*) FILTER (WHERE trust_score IS NOT NULL AND trust_score < 0.4) AS low_trust
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
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'ad_intel_scan','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"channel": channel}), label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("ad_intel_add_channel uid=%d", uid)
            return _err(str(exc), 500)

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
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET bot_role=$1, cluster=$2 WHERE bot_id=$3 AND added_by=$4",
                role, cluster, bot_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("set_bot_role_api uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    # ── Relay (Inbox) ─────────────────────────────────────────────────────────

    async def relay_sessions_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            owned = await pool.fetchval(
                "SELECT 1 FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid
            )
        except Exception as exc:
            log.exception("relay_sessions_list ownership uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
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
        try:
            row = await pool.fetchrow(
                """SELECT rs.id FROM relay_sessions rs
                   JOIN managed_bots mb ON mb.bot_id=rs.bot_id
                   WHERE rs.id=$1 AND mb.added_by=$2""",
                session_id, uid,
            )
        except Exception as exc:
            log.exception("relay_session_messages ownership uid=%d sess=%d", uid, session_id)
            return _err(str(exc), 500)
        if not row:
            return _err("Not found", 404)
        try:
            msgs = await pool.fetch(
                "SELECT id, direction, text, created_at FROM relay_messages "
                "WHERE session_id=$1 ORDER BY created_at ASC LIMIT 100",
                session_id,
            )
        except Exception as exc:
            log.exception("relay_session_messages msgs uid=%d sess=%d", uid, session_id)
            return _err(str(exc), 500)
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
        try:
            res = await pool.execute(
                "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2 AND added_by=$3",
                enabled, bot_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True, "relay_enabled": enabled})
        except Exception as exc:
            log.exception("relay_toggle uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

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
        try:
            res = await pool.execute(
                "UPDATE api_keys SET is_active=FALSE WHERE id=$1 AND user_id=$2",
                key_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("revoke_api_key uid=%d key=%d", uid, key_id)
            return _err(str(exc), 500)

    async def create_api_key(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body.get("name") or "Mini App Key").strip()[:64]
        import secrets, hashlib
        raw_key = secrets.token_urlsafe(32)
        prefix = raw_key[:8]
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        try:
            row = await pool.fetchrow(
                """INSERT INTO api_keys(user_id, key_hash, key_prefix, name)
                   VALUES($1,$2,$3,$4) RETURNING id""",
                uid, key_hash, prefix, name,
            )
            return _json_resp({"ok": True, "id": row["id"], "key": raw_key, "prefix": prefix, "name": name})
        except Exception as exc:
            log.exception("create_api_key uid=%d", uid)
            return _err(str(exc), 500)

    # ── Multigeo (per-language bot profile) ───────────────────────────────────

    async def multigeo_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
                bot_id, uid,
            )
        except Exception as e:
            log.warning("multigeo_get db error: %s", e)
            return _err("db error", 500)
        if not row:
            return _err("bot not found", 404)
        import aiohttp as _aiohttp
        from services import bot_api as _bapi
        langs = ["", "ru", "en", "de", "fr", "es", "it", "uk", "pt", "zh", "ar"]
        result = []
        try:
            async with _aiohttp.ClientSession() as session:
                for lc in langs:
                    try:
                        name = await _bapi.get_my_name(session, row["token"], lc)
                        desc = await _bapi.get_my_description(session, row["token"], lc)
                        short = await _bapi.get_my_short_description(session, row["token"], lc)
                        if name or desc or short:
                            result.append({"lang": lc or "default", "name": name, "description": desc, "short_description": short})
                    except Exception:
                        pass
        except Exception as exc:
            return _err(str(exc), 500)
        return _json_resp({"profiles": result})

    async def multigeo_set(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        lang = (body.get("lang") or "").strip()
        name = (body.get("name") or "").strip()[:64]
        description = (body.get("description") or "").strip()[:512]
        short_description = (body.get("short_description") or "").strip()[:120]
        if lang == "default":
            lang = ""
        try:
            row = await pool.fetchrow(
                "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
                bot_id, uid,
            )
        except Exception as e:
            log.warning("multigeo_set db error: %s", e)
            return _err("db error", 500)
        if not row:
            return _err("bot not found", 404)
        import aiohttp as _aiohttp
        from services import bot_api as _bapi
        errors = []
        try:
            async with _aiohttp.ClientSession() as session:
                if name:
                    ok = await _bapi.set_name(session, row["token"], name, lang)
                    if not ok:
                        errors.append("name")
                if description:
                    ok = await _bapi.set_description(session, row["token"], description, lang)
                    if not ok:
                        errors.append("description")
                if short_description:
                    ok = await _bapi.set_short_description(session, row["token"], short_description, lang)
                    if not ok:
                        errors.append("short_description")
        except Exception as exc:
            return _err(str(exc), 500)
        if errors:
            return _json_resp({"ok": False, "errors": errors})
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

    # ── Strike: status + launch ────────────────────────────────────────────────

    async def strike_status(request: web.Request) -> web.Response:
        """Проверяет доступ к Strike и возвращает список категорий."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                "SELECT purchased_at, mode FROM strike_access WHERE user_id=$1", uid
            )
            from services.strike_engine import MINI_CATEGORIES
            categories = [
                {"key": k, "label": v["label"], "severity": v.get("severity", "MEDIUM")}
                for k, v in MINI_CATEGORIES.items()
            ]
            return _json_resp({
                "has_access": row is not None,
                "mode": row["mode"] if row else None,
                "purchased_at": row["purchased_at"].isoformat() if row and row["purchased_at"] else None,
                "categories": categories,
            })
        except Exception as exc:
            log.exception("strike_status uid=%d", uid)
            return _err(str(exc), 500)

    async def strike_launch(request: web.Request) -> web.Response:
        """Создаёт Strike операцию и ставит её в очередь."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            target = (body.get("target") or "").strip()
            category = (body.get("category") or "").strip()
            if not target or len(target) < 3:
                return _err("Укажите цель (username или ссылку)", 400)
            if not category:
                return _err("Выберите категорию нарушения", 400)

            # Проверяем доступ
            row = await pool.fetchrow(
                "SELECT mode FROM strike_access WHERE user_id=$1", uid
            )
            if not row:
                return _err("Нет доступа к Strike. Необходима лицензия.", 403)

            from services.strike_engine import MINI_CATEGORIES
            cat = MINI_CATEGORIES.get(category)
            if not cat:
                return _err("Неизвестная категория", 400)

            # Нормализация target
            from services.account_manager import normalize_telegram_join_ref
            ref_kind, ref_value = normalize_telegram_join_ref(target)
            normalized = f"+{ref_value}" if ref_kind == "invite" else ref_value.lstrip("@")
            if not normalized or len(normalized) < 3:
                return _err("Некорректный username или ссылка", 400)

            # Подсчёт доступных аккаунтов
            accs = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=true
                     AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired')
                   LIMIT 50""",
                uid,
            )
            if not accs:
                return _err("Нет доступных активных аккаунтов для Strike", 400)

            # Создаём операцию в очереди (op_type='strike' — воркер знает этот тип)
            op_id = await pool.fetchval(
                """INSERT INTO operation_queue(owner_id, op_type, label, status, params, total_items)
                   VALUES($1, 'strike', $2, 'pending', $3::jsonb, $4)
                   RETURNING id""",
                uid,
                f"Strike: {normalized} [{cat['label']}]",
                __import__("json").dumps({
                    "target": normalized,
                    "reason": cat["tg_reason"],
                    "account_ids": [r["id"] for r in accs],
                }),
                len(accs),
            )
            return _json_resp({"ok": True, "operation_id": op_id, "accounts": len(accs)})
        except Exception as exc:
            log.exception("strike_launch uid=%d", uid)
            return _err(str(exc), 500)

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
        try:
            limit = min(int(request.rel_url.query.get("limit", "50")), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = int(request.rel_url.query.get("offset", "0"))
        except (ValueError, TypeError):
            offset = 0
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
        if parse_type not in ("members", "active"):
            parse_type = "members"
        if limit < 1 or limit > 10000:
            limit = 500
        import json as _json
        label = f"Парсинг {parse_type} из @{source_ref} (до {limit})"
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'parse_audience','pending',$2,$3,$4) RETURNING id",
                uid,
                _json.dumps({"source_ref": source_ref, "parse_type": parse_type, "limit": limit}),
                limit, label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("submit_parse_job uid=%d", uid)
            return _err(str(exc), 500)

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
        try:
            summary_rows = await pool.fetch(
                "SELECT stage, COUNT(*) AS cnt, COALESCE(SUM(value),0) AS total "
                "FROM crm_deals WHERE owner_id=$1 GROUP BY stage",
                uid,
            )
        except Exception:
            summary_rows = []
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
        try:
            res = await pool.execute(
                "UPDATE crm_deals SET stage=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
                stage, deal_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("update_crm_deal_stage uid=%d deal=%d", uid, deal_id)
            return _err(str(exc), 500)

    async def delete_crm_deal(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            deal_id = int(request.match_info["deal_id"])
        except (KeyError, ValueError):
            return _err("bad deal_id", 400)
        try:
            res = await pool.execute(
                "DELETE FROM crm_deals WHERE id=$1 AND owner_id=$2", deal_id, uid
            )
            if res == "DELETE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("delete_crm_deal uid=%d deal=%d", uid, deal_id)
            return _err(str(exc), 500)

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
        try:
            role = await pool.fetchval(
                "SELECT role FROM workspace_members WHERE workspace_id=$1 AND user_id=$2",
                ws_id, uid,
            )
        except Exception as exc:
            log.exception("leave_workspace role uid=%d ws=%d", uid, ws_id)
            return _err(str(exc), 500)
        if not role:
            return _err("Not a member", 404)
        try:
            if role == "owner":
                # Delete workspace entirely
                await pool.execute("DELETE FROM workspaces WHERE id=$1 AND owner_id=$2", ws_id, uid)
            else:
                await pool.execute(
                    "DELETE FROM workspace_members WHERE workspace_id=$1 AND user_id=$2", ws_id, uid
                )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("leave_workspace delete uid=%d ws=%d", uid, ws_id)
            return _err(str(exc), 500)

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
        try:
            res = await pool.execute(
                "UPDATE promo_orders SET status='cancelled', updated_at=NOW() WHERE id=$1 AND owner_id=$2",
                order_id, uid,
            )
            if res == "UPDATE 0":
                return _err("Not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("promo_cancel_order uid=%d order=%d", uid, order_id)
            return _err(str(exc), 500)

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
        try:
            rows = await pool.fetch(
                "SELECT id, description, status, created_at FROM error_reports "
                "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
                uid,
            )
        except Exception as exc:
            log.exception("my_error_reports uid=%d", uid)
            return _err(str(exc), 500)
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
        # Опциональный выбор ботов: bot_ids → рассылка только выбранным
        sel_ids = [int(x) for x in (body.get("bot_ids") or []) if str(x).lstrip("-").isdigit()]
        if sel_ids:
            bots = await _safe_fetch(pool,
                """SELECT mb.bot_id, COUNT(bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
                   FROM managed_bots mb
                   LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
                   WHERE mb.added_by=$1 AND mb.is_active=true AND mb.bot_id = ANY($2::bigint[])
                   GROUP BY mb.bot_id""", uid, sel_ids)
        else:
            bots = await _safe_fetch(pool,
                """SELECT mb.bot_id, COUNT(bu.user_id) FILTER (WHERE bu.is_active=true) AS active_subs
                   FROM managed_bots mb
                   LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
                   WHERE mb.added_by=$1 AND mb.is_active=true
                   GROUP BY mb.bot_id""", uid)
        if not bots:
            return _err("No active bots found")
        total_recipients = sum(b["active_subs"] or 0 for b in bots)
        lang = body.get("lang", "")
        # Инлайн-кнопки (необязательно)
        _buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = str(b.get("text") or "").strip()[:64]
                bu = str(b.get("url") or "").strip()
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                _buttons.append({"text": bt, "url": bu})
        if sel_ids:
            segment = "selected_bots"
            op_params = {"text": text, "segment": segment, "lang": lang,
                         "selected_bot_ids": [int(b["bot_id"]) for b in bots]}
        else:
            segment = body.get("segment", "all_each")
            op_params = {"text": text, "segment": segment, "lang": lang}
        if _buttons:
            op_params["buttons"] = _buttons
        label = f"Network Broadcast: {text[:40]}{'…' if len(text) > 40 else ''}"
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'network_broadcast','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps(op_params),
                total_recipients, label,
            )
            return _json_resp({
                "ok": True,
                "op_id": op_id,
                "total_recipients": total_recipients,
                "broadcasts_created": len(bots),
                "label": label,
            })
        except Exception:
            log.exception("network_broadcast op_queue uid=%d", uid)
            return _err("Failed to queue broadcast", 500)

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

    async def crm_contact_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        first_name = str(data.get("first_name", "")).strip()
        last_name = str(data.get("last_name", "")).strip() or None
        username = str(data.get("username", "")).strip().lstrip("@") or None
        phone = str(data.get("phone", "")).strip() or None
        tg_user_id_raw = data.get("tg_user_id")
        tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
        notes = str(data.get("notes", "")).strip() or None
        if not first_name and not username and not phone:
            return _err("Укажите имя, @username или номер телефона", 400)
        tg_user_id = int(tg_user_id_raw) if tg_user_id_raw else None
        if not tg_user_id:
            import random as _rand
            tg_user_id = _rand.randint(2_000_000_000, 9_000_000_000)
        try:
            cid = await pool.fetchval(
                """INSERT INTO crm_contacts
                   (owner_id, tg_user_id, first_name, last_name, username, phone, tags, notes, source)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'manual')
                   ON CONFLICT (owner_id, tg_user_id) DO UPDATE
                     SET first_name=EXCLUDED.first_name,
                         last_name=EXCLUDED.last_name,
                         username=EXCLUDED.username,
                         phone=EXCLUDED.phone,
                         tags=EXCLUDED.tags,
                         notes=EXCLUDED.notes,
                         updated_at=now()
                   RETURNING id""",
                uid, tg_user_id, first_name or None, last_name, username, phone,
                tags, notes,
            )
            return _json_resp({"id": cid, "ok": True})
        except Exception as exc:
            log.exception("crm_contact_create uid=%d", uid)
            return _err(str(exc), 500)

    async def crm_contact_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            contact_id = int(request.match_info["contact_id"])
        except (KeyError, ValueError):
            return _err("bad contact_id", 400)
        try:
            await pool.execute(
                "DELETE FROM crm_contacts WHERE id=$1 AND owner_id=$2", contact_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("crm_contact_delete uid=%d cid=%d", uid, contact_id)
            return _err(str(exc), 500)

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
        # Values must match account_warmer.create_warmup_plan() — aggressive capped at 12/day
        target_days    = {"gentle": 21, "standard": 14, "aggressive": 10}[plan_type]
        daily_actions  = {"gentle":  5, "standard": 10, "aggressive": 12}[plan_type]
        # Require session_str so the worker doesn't fail immediately
        has_session = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND session_str IS NOT NULL",
            acc_id, uid)
        if not has_session:
            return _err("Аккаунт не имеет активной сессии — сначала добавьте .session файл", 400)
        # Cancel any active warmup first
        try:
            await pool.execute(
                "UPDATE account_warmup_plans SET status='paused' WHERE account_id=$1 AND owner_id=$2 AND status='active'",
                acc_id, uid)
            row = await pool.fetchrow(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (account_id) DO UPDATE
                   SET plan_type=$3, target_days=$4, daily_actions=$5, status='active', started_at=NOW()
                   RETURNING id""",
                uid, acc_id, plan_type, target_days, daily_actions)
            # Also create op_queue entry so op_worker executes warmup logic
            label = f"Прогрев аккаунта #{acc_id} ({plan_type})"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'account_warmup','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({"account_id": acc_id, "plan_type": plan_type}), label,
            )
            return _json_resp({"ok": True, "id": row["id"], "op_id": op_id, "target_days": target_days})
        except Exception as exc:
            log.exception("start_warmup acc=%d uid=%d", acc_id, uid)
            return _err(f"Ошибка запуска прогрева: {exc}", 500)

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
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM asset_templates WHERE owner_id=$1 AND asset_type='post'", uid)
        return _json_resp({"templates": rows, "total": int(total or 0)})

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
        # Источник истины — тот же, что у бота: активная не истёкшая подписка в
        # subscriptions (get_plan). Раньше Mini App читал platform_users.current_plan,
        # который часть платёжных путей не обновляет → приложение показывало «free»
        # при реально оплаченном тарифе.
        plan = None
        try:
            from bot.utils.subscription import get_plan as _gp
            plan = await _gp(pool, uid)
        except Exception:
            plan = None
        expires = None
        is_active = False
        try:
            srow = await pool.fetchrow(
                "SELECT plan, expires_at FROM subscriptions "
                "WHERE user_id=$1 AND is_active=true AND expires_at > now() "
                "ORDER BY expires_at DESC LIMIT 1", uid)
            if srow:
                is_active = True
                expires = str(srow["expires_at"])
                if not plan or plan == "free":
                    plan = srow["plan"] or plan
        except Exception:
            pass
        # Фолбэк на platform_users только если источник истины недоступен.
        if not plan:
            try:
                prow = await pool.fetchrow(
                    "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid)
                if prow:
                    plan = prow["current_plan"] or "free"
                    if not expires and prow["plan_expires_at"]:
                        expires = str(prow["plan_expires_at"])
            except Exception:
                pass
        # Нормализуем к бинарной модели (starter/pro/enterprise → paid),
        # чтобы клиент корректно показал платный тариф.
        try:
            from bot.utils.subscription import coerce_plan as _cp
            plan = _cp(plan or "free")
        except Exception:
            plan = plan or "free"
        return _json_resp({
            "plan": plan,
            "expires_at": expires,
            "is_active": is_active or plan != "free",
        })

    async def user_settings_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            row = await pool.fetchrow(
                "SELECT settings_json FROM platform_users WHERE user_id=$1", uid)
            if row and row["settings_json"]:
                import json as _json
                return _json_resp(_json.loads(row["settings_json"]))
        except Exception:
            pass
        return _json_resp({
            "notif_ops": True,
            "notif_pay": True,
            "notif_report": False,
            "notif_error": True,
            "utc_logs": False,
            "lang": "ru",
        })

    async def user_settings_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
            import json as _json
            settings_json = _json.dumps(data)
            await pool.execute(
                """UPDATE platform_users SET settings_json=$1 WHERE user_id=$2""",
                settings_json, uid)
            return _json_resp({"ok": True})
        except Exception as e:
            log.warning("user_settings_save uid=%d: %s", uid, e)
            return _err("save failed", 500)

    async def payments_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT plan, period_months, amount_usd, currency, status, created_at
                   FROM payments
                   WHERE user_id=$1
                   ORDER BY created_at DESC
                   LIMIT 20""",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception:
            return _json_resp([])

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
            return _err("Unauthorized", 401)
        asset_type = request.rel_url.query.get("type")
        try:
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
        except Exception as exc:
            log.exception("asset_templates_list uid=%d", uid)
            return _err(str(exc), 500)

    async def asset_template_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("bad tpl_id", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT * FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if not tpl:
                return _err("not found", 404)
            return _json_resp(dict(tpl))
        except Exception as exc:
            log.exception("asset_template_detail uid=%d tpl=%d", uid, tpl_id)
            return _err(str(exc), 500)

    async def asset_template_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("bad tpl_id", 400)
        try:
            result = await pool.execute(
                "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if result == "DELETE 0":
                return _err("not found", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("asset_template_delete uid=%d tpl=%d", uid, tpl_id)
            return _err(str(exc), 500)

    # ── Infra Health Center ───────────────────────────────────────────────────

    async def infra_health_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            alerts = await pool.fetch(
                "SELECT id, alert_type, severity, title, description, target_type, "
                "is_active, first_seen_at, resolved_at "
                "FROM infrastructure_alerts WHERE owner_id=$1 AND is_active=TRUE "
                "ORDER BY first_seen_at DESC LIMIT 20",
                uid,
            )
        except Exception:
            log.exception("infra_health_overview alerts uid=%d", uid)
            alerts = []
        try:
            recovery = await pool.fetch(
                "SELECT id, recovery_type, target_type, trigger, action, status, "
                "severity, created_at, completed_at "
                "FROM recovery_events WHERE owner_id=$1 "
                "ORDER BY created_at DESC LIMIT 20",
                uid,
            )
        except Exception:
            log.exception("infra_health_overview recovery uid=%d", uid)
            recovery = []
        return _json_resp({
            "alerts": [dict(a) for a in alerts],
            "recovery": [dict(r) for r in recovery],
        })

    # ── Swarm ─────────────────────────────────────────────────────────────────

    async def swarm_metrics(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("swarm_metrics uid=%d", uid)
            return _err(str(exc), 500)

    # ── Presence Packs ────────────────────────────────────────────────────────

    async def presence_packs_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, name, description, target_url, target_label, bot_id "
                "FROM presence_packs WHERE owner_id=$1 ORDER BY id DESC LIMIT 30",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("presence_packs_list uid=%d", uid)
            return _err(str(exc), 500)

    async def presence_pack_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        target_url = (body.get("target_url") or "").strip()
        target_label = (body.get("target_label") or "").strip()
        bot_id = body.get("bot_id") or None
        if not name:
            return _err("name required")
        if bot_id:
            try:
                bot_id = int(bot_id)
            except (TypeError, ValueError):
                bot_id = None
        try:
            row = await pool.fetchrow(
                """INSERT INTO presence_packs(owner_id, name, description, target_url, target_label, bot_id)
                   VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
                uid, name, description or None, target_url or None, target_label or None, bot_id,
            )
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as exc:
            log.exception("presence_pack_create uid=%d", uid)
            return _err(str(exc), 500)

    async def presence_pack_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("bad pack_id", 400)
        try:
            await pool.execute(
                "DELETE FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("presence_pack_delete uid=%d", uid)
            return _err(str(exc), 500)

    # ── Global Presence ───────────────────────────────────────────────────────

    async def global_presence_plans(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("global_presence_plans uid=%d", uid)
            return _err(str(exc), 500)

    async def global_presence_plan_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("bad plan_id", 400)
        try:
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
        except Exception as exc:
            log.exception("global_presence_plan_detail uid=%d plan=%d", uid, plan_id)
            return _err(str(exc), 500)

    # ── Mass Ops ──────────────────────────────────────────────────────────────

    async def mass_ops_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("mass_ops_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Ecosystems ────────────────────────────────────────────────────────────

    async def ecosystems_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("ecosystems_list uid=%d", uid)
            return _err(str(exc), 500)

    async def ecosystem_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("bad eco_id", 400)
        try:
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
            try:
                events = await pool.fetch(
                    "SELECT event_type, severity, title, occurred_at "
                    "FROM ecosystem_events WHERE ecosystem_id=$1 ORDER BY occurred_at DESC LIMIT 20",
                    eco_id,
                )
            except Exception:
                events = []
            return _json_resp({
                "eco": dict(eco),
                "members": [dict(m) for m in members],
                "events": [dict(ev) for ev in events],
            })
        except Exception as exc:
            log.exception("ecosystem_detail uid=%d eco=%d", uid, eco_id)
            return _err(str(exc), 500)

    async def ecosystem_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        name = (body.get("name") or "").strip()
        description = (body.get("description") or "").strip()
        ecosystem_type = (body.get("ecosystem_type") or "custom").strip()
        region = (body.get("region") or "").strip()
        if not name:
            return _err("name required")
        try:
            row = await pool.fetchrow(
                """INSERT INTO ecosystems(owner_id, name, description, ecosystem_type, region)
                   VALUES($1,$2,$3,$4,$5) RETURNING id""",
                uid, name, description or None, ecosystem_type, region or None,
            )
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as exc:
            log.exception("ecosystem_create uid=%d", uid)
            return _err(str(exc), 500)

    async def ecosystem_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("bad eco_id", 400)
        try:
            await pool.execute(
                "DELETE FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("ecosystem_delete uid=%d eco=%d", uid, eco_id)
            return _err(str(exc), 500)

    # ── Channel Factory ───────────────────────────────────────────────────────

    async def channel_factory_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad body", 400)
        title = (body.get("title") or "").strip()
        about = (body.get("about") or "").strip()
        account_id = body.get("account_id")
        if not title:
            return _err("title required")
        if not account_id:
            return _err("account_id required")
        try:
            acc = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
                int(account_id), uid,
            )
            if not acc:
                return _err("Аккаунт не найден или неактивен", 404)
            from bot.utils.subscription import get_channel_limit, get_effective_channel_count
            _lim = await get_channel_limit(pool, uid)
            if await get_effective_channel_count(pool, uid) >= _lim:
                return _err(f"Достигнут лимит каналов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'create_channel','pending',$2,1,$3) RETURNING id",
                uid, json.dumps({"title": title, "about": about, "account_id": account_id}),
                f"Создать канал: {title}",
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("channel_factory_submit uid=%d", uid)
            return _err(str(exc), 500)

    async def channel_factory_recent(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, title, username, type, added_at FROM managed_channels "
                "WHERE owner_id=$1 ORDER BY added_at DESC LIMIT 20",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("channel_factory_recent uid=%d", uid)
            return _err(str(exc), 500)

    # ── Group Factory ─────────────────────────────────────────────────────────

    async def group_factory_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad body", 400)
        title = (body.get("title") or "").strip()
        account_id = body.get("account_id")
        is_supergroup = body.get("is_supergroup", True)
        if not title:
            return _err("title required")
        if not account_id:
            return _err("account_id required")
        try:
            acc = await pool.fetchrow(
                "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE",
                int(account_id), uid,
            )
            if not acc:
                return _err("Аккаунт не найден или неактивен", 404)
            from bot.utils.subscription import get_channel_limit, get_effective_channel_count
            _lim = await get_channel_limit(pool, uid)
            if await get_effective_channel_count(pool, uid) >= _lim:
                return _err(f"Достигнут лимит каналов/групп ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'create_group','pending',$2,1,$3) RETURNING id",
                uid, json.dumps({"title": title, "account_id": account_id, "is_supergroup": is_supergroup}),
                f"Создать группу: {title}",
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("group_factory_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Physics Hub ──────────────────────────────────────────────────────────

    async def physics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("physics_overview uid=%d", uid)
            return _err(str(exc), 500)

    async def physics_account_telemetry(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            account_id = int(request.match_info["account_id"])
        except (KeyError, ValueError):
            return _err("bad account_id", 400)
        try:
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
        except Exception as exc:
            log.exception("physics_account_telemetry uid=%d acc=%d", uid, account_id)
            return _err(str(exc), 500)

    # ── Graph Hub ─────────────────────────────────────────────────────────────

    async def graph_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Filter via owner's known channels (graph_nodes has no owner_id)
            stats = await pool.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM graph_nodes gn
                     WHERE EXISTS (
                         SELECT 1 FROM managed_channels mc
                         WHERE mc.owner_id=$1
                           AND (mc.username = gn.username OR mc.channel_id::text = gn.entity_id)
                     )) AS nodes,
                    (SELECT COUNT(*) FROM graph_edges ge
                     JOIN graph_nodes na ON na.id = ge.from_node
                     WHERE EXISTS (
                         SELECT 1 FROM managed_channels mc
                         WHERE mc.owner_id=$1
                           AND (mc.username = na.username OR mc.channel_id::text = na.entity_id)
                     )) AS edges,
                    (SELECT COUNT(*) FROM audience_overlaps ao
                     JOIN graph_nodes na ON na.id = ao.node_a
                     WHERE ao.overlap_pct > 0.1
                       AND EXISTS (
                           SELECT 1 FROM managed_channels mc
                           WHERE mc.owner_id=$1
                             AND (mc.username = na.username OR mc.channel_id::text = na.entity_id)
                       )) AS strong_overlaps
                """,
                uid,
            )
            return _json_resp(dict(stats) if stats else {"nodes": 0, "edges": 0, "strong_overlaps": 0})
        except Exception as exc:
            log.exception("graph_stats uid=%d", uid)
            return _err(str(exc), 500)

    async def graph_overlaps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """
                SELECT ao.overlap_pct, ao.shared_users, ao.computed_at,
                       na.title AS title_a, na.username AS username_a,
                       nb.title AS title_b, nb.username AS username_b
                FROM audience_overlaps ao
                JOIN graph_nodes na ON na.id = ao.node_a
                JOIN graph_nodes nb ON nb.id = ao.node_b
                WHERE ao.overlap_pct > 0.05
                  AND EXISTS (
                      SELECT 1 FROM managed_channels mc
                      WHERE mc.owner_id=$1
                        AND (mc.username = na.username OR mc.username = nb.username
                             OR mc.channel_id::text = na.entity_id OR mc.channel_id::text = nb.entity_id)
                  )
                ORDER BY ao.overlap_pct DESC LIMIT 20
                """,
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("graph_overlaps uid=%d", uid)
            return _err(str(exc), 500)

    # ── Compliance Hub ────────────────────────────────────────────────────────

    async def compliance_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("compliance_overview uid=%d", uid)
            return _err(str(exc), 500)

    # ── Content Cloner ───────────────────────────────────────────────────────

    async def content_cloner_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, op_type, status, params, COALESCE(label, op_type) AS label, created_at FROM operation_queue "
                "WHERE owner_id=$1 AND op_type='content_clone' ORDER BY created_at DESC LIMIT 20",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("content_cloner_history uid=%d", uid)
            return _err(str(exc), 500)

    async def content_cloner_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        source = (body.get("source") or "").strip()
        if not source:
            return _err("source required")
        account_id = body.get("account_id")
        # Resolve the worker's contract here: _exec_content_clone expects source_ref,
        # a list of target channels and an explicit account list. The Mini App only
        # collects a source, so derive the rest — clone the source channel's recent
        # posts into the user's own managed channels using an active account.
        if account_id:
            try:
                account_ids = [int(account_id)]
            except (TypeError, ValueError):
                account_ids = []
        else:
            acc_rows = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                "AND session_str IS NOT NULL "
                "AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired') "
                "ORDER BY last_used DESC NULLS LAST LIMIT 1", uid)
            account_ids = [r["id"] for r in acc_rows]
        if not account_ids:
            return _err("Нет активного аккаунта с сессией для клонирования", 400)
        chan_rows = await _safe_fetch(pool,
            "SELECT username, channel_id FROM managed_channels WHERE owner_id=$1", uid)
        target_refs = [
            ("@" + r["username"]) if r["username"] else r["channel_id"]
            for r in chan_rows
        ]
        if not target_refs:
            return _err("Нет управляемых каналов — добавьте канал, куда клонировать контент", 400)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'content_clone','pending',$2,$3,$4) RETURNING id",
                uid, json.dumps({
                    "source": source,          # kept for history display (JS reads payload.source)
                    "source_ref": source,      # read by _exec_content_clone
                    "target_refs": target_refs,
                    "account_ids": account_ids,
                    "mode": "forward",
                    "msg_count": 10,
                }),
                len(target_refs),
                f"Клонировать контент: {source} → {len(target_refs)} канал(ов)",
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("content_cloner_submit uid=%d", uid)
            return _err(str(exc), 500)

    # ── Clone Adapt ───────────────────────────────────────────────────────────

    async def clone_adapt_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("clone_adapt_history uid=%d", uid)
            return _err(str(exc), 500)

    # ── Content Mesh ──────────────────────────────────────────────────────────

    async def content_meshes_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("content_meshes_list uid=%d", uid)
            return _err(str(exc), 500)

    async def content_mesh_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("bad mesh_id", 400)
        try:
            mesh = await pool.fetchrow(
                "SELECT enabled FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid
            )
            if not mesh:
                return _err("not found", 404)
            new_state = not mesh["enabled"]
            await pool.execute(
                "UPDATE content_meshes SET enabled=$1, updated_at=NOW() WHERE id=$2 AND owner_id=$3", new_state, mesh_id, uid
            )
            return _json_resp({"enabled": new_state})
        except Exception as exc:
            log.exception("content_mesh_toggle uid=%d mesh=%d", uid, mesh_id)
            return _err(str(exc), 500)

    async def clone_adapt_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        source_bot_id = data.get("source_bot_id")
        target_bot_id = data.get("target_bot_id")
        fields = data.get("fields", "name,desc")
        if not source_bot_id or not target_bot_id:
            return _err("source_bot_id и target_bot_id обязательны", 400)
        if int(source_bot_id) == int(target_bot_id):
            return _err("Источник и цель должны быть разными ботами", 400)
        try:
            src = await pool.fetchrow(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                int(source_bot_id), uid,
            )
            tgt = await pool.fetchrow(
                "SELECT bot_id, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                int(target_bot_id), uid,
            )
            if not src:
                return _err("Исходный бот не найден", 404)
            if not tgt:
                return _err("Целевой бот не найден", 404)
            src_name = src["username"] or src["first_name"] or f"id{src['bot_id']}"
            tgt_name = tgt["username"] or tgt["first_name"] or f"id{tgt['bot_id']}"
            op_id = await pool.fetchval(
                """INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label)
                   VALUES($1,'clone_adapt','pending',$2,1,$3) RETURNING id""",
                uid,
                _json.dumps({
                    "source_bot_id": int(source_bot_id),
                    "target_bot_id": int(target_bot_id),
                    "fields": fields,
                }),
                f"Clone: @{src_name} → @{tgt_name}",
            )
            return _json_resp({"op_id": op_id, "ok": True})
        except Exception as exc:
            log.exception("clone_adapt_submit uid=%d", uid)
            return _err(str(exc), 500)

    async def content_mesh_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        name = str(data.get("name", "")).strip()
        source_channel = str(data.get("source_channel", "")).strip() or None
        source_account_id = data.get("source_account_id")
        delay_minutes = int(data.get("delay_minutes", 30))
        append_text = str(data.get("append_text", "")).strip() or None
        if not name:
            return _err("name обязателен", 400)
        if delay_minutes < 1:
            delay_minutes = 1
        try:
            mid = await pool.fetchval(
                """INSERT INTO content_meshes
                   (owner_id, name, source_channel, source_account_id, delay_minutes, append_text)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                uid, name, source_channel,
                int(source_account_id) if source_account_id else None,
                delay_minutes, append_text,
            )
            return _json_resp({"id": mid, "ok": True})
        except Exception as exc:
            log.exception("content_mesh_create uid=%d", uid)
            return _err(str(exc), 500)

    # ── Narrative Engine ──────────────────────────────────────────────────────

    async def narrative_campaigns_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, topic, campaign_type, spread_hours, posts_total, posts_published, status, created_at "
                "FROM narrative_campaigns WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 30",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("narrative_campaigns_list uid=%d", uid)
            return _err(str(exc), 500)

    async def narrative_campaign_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info["campaign_id"])
        except (KeyError, ValueError):
            return _err("bad campaign_id", 400)
        try:
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
        except Exception as exc:
            log.exception("narrative_campaign_detail uid=%d cid=%d", uid, cid)
            return _err(str(exc), 500)

    async def narrative_campaign_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        topic = str(data.get("topic", "")).strip()
        core_message = str(data.get("core_message", "")).strip()
        campaign_type = data.get("campaign_type", "trend")
        spread_hours = int(data.get("spread_hours", 4))
        if not topic or not core_message:
            return _err("topic и core_message обязательны", 400)
        if campaign_type not in ("trend", "launch", "awareness", "counter"):
            campaign_type = "trend"
        if spread_hours < 1:
            spread_hours = 1
        try:
            cid = await pool.fetchval(
                """INSERT INTO narrative_campaigns
                   (owner_id, topic, core_message, campaign_type, spread_hours, status)
                   VALUES ($1, $2, $3, $4, $5, 'draft') RETURNING id""",
                uid, topic, core_message, campaign_type, spread_hours,
            )
            return _json_resp({"id": cid, "ok": True})
        except Exception as exc:
            log.exception("narrative_campaign_create uid=%d", uid)
            return _err(str(exc), 500)

    # ── Self Promo ───────────────────────────────────────────────────────────

    async def self_promo_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT id, style, title, content, cta_text, cta_url, add_referral, is_active, use_count, "
                "CASE WHEN owner_id=$1 THEN true ELSE false END AS is_mine "
                "FROM self_promo_templates WHERE owner_id=$1 OR owner_id IS NULL ORDER BY id",
                uid,
            )
            return _json_resp([dict(r) for r in rows])
        except Exception as exc:
            log.exception("self_promo_list uid=%d", uid)
            return _err(str(exc), 500)

    async def self_promo_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        style = data.get("style", "direct")
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        cta_text = str(data.get("cta_text", "")).strip() or None
        cta_url = str(data.get("cta_url", "")).strip() or None
        add_referral = bool(data.get("add_referral", False))
        if not title or not content:
            return _err("title и content обязательны", 400)
        if style not in ("direct", "native"):
            style = "direct"
        if len(content) > 4096:
            return _err("content слишком длинный (макс 4096 символов)", 400)
        try:
            tid = await pool.fetchval(
                """INSERT INTO self_promo_templates
                   (owner_id, style, title, content, cta_text, cta_url, add_referral)
                   VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
                uid, style, title, content, cta_text, cta_url, add_referral,
            )
            return _json_resp({"id": tid, "ok": True})
        except Exception as exc:
            log.exception("self_promo_create uid=%d", uid)
            return _err(str(exc), 500)

    async def self_promo_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("bad tpl_id", 400)
        try:
            result = await pool.execute(
                "DELETE FROM self_promo_templates WHERE id=$1 AND owner_id=$2", tpl_id, uid
            )
            if result == "DELETE 0":
                return _err("Шаблон не найден или нельзя удалить системный шаблон", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("self_promo_delete uid=%d tpl=%d", uid, tpl_id)
            return _err(str(exc), 500)

    async def self_promo_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("bad tpl_id", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT is_active, owner_id FROM self_promo_templates WHERE id=$1 AND (owner_id=$2 OR owner_id IS NULL)",
                tpl_id, uid,
            )
            if not tpl:
                return _err("not found", 404)
            # Системные (общие) шаблоны нельзя переключать обычному пользователю —
            # это влияло бы на всех. Меняем только свои.
            if tpl["owner_id"] is None:
                return _err("Системный шаблон нельзя переключать", 403)
            new_state = not tpl["is_active"]
            await pool.execute(
                "UPDATE self_promo_templates SET is_active=$1 WHERE id=$2 AND owner_id=$3",
                new_state, tpl_id, uid,
            )
            return _json_resp({"active": new_state})
        except Exception as exc:
            log.exception("self_promo_toggle uid=%d tpl=%d", uid, tpl_id)
            return _err(str(exc), 500)

    async def self_promo_launch(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            tpl_id = int(request.match_info["tpl_id"])
        except (KeyError, ValueError):
            return _err("bad tpl_id", 400)
        try:
            tpl = await pool.fetchrow(
                "SELECT id, title FROM self_promo_templates "
                "WHERE id=$1 AND is_active AND (owner_id=$2 OR owner_id IS NULL)",
                tpl_id, uid,
            )
            if not tpl:
                return _err("Шаблон не найден или неактивен", 404)
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'self_promo_blast','pending',$2,1,$3) RETURNING id",
                uid, json.dumps({"template_id": tpl_id}),
                f"Self-promo: {tpl['title'] or tpl_id}",
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("self_promo_launch uid=%d tpl=%d", uid, tpl_id)
            return _err(str(exc), 500)

    # ── Semantic Memory ───────────────────────────────────────────────────────

    async def semantic_memory_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("semantic_memory_overview uid=%d", uid)
            return _err(str(exc), 500)

    async def semantic_memory_bot(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            owner = await pool.fetchval("SELECT added_by FROM managed_bots WHERE bot_id=$1", bot_id)
            if owner != uid:
                return _err("forbidden", 403)
            facts = await pool.fetch(
                "SELECT user_id, fact_key, fact_value, confidence, updated_at "
                "FROM bot_user_facts WHERE bot_id=$1 ORDER BY updated_at DESC LIMIT 100",
                bot_id,
            )
            return _json_resp([dict(r) for r in facts])
        except Exception as exc:
            log.exception("semantic_memory_bot uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    # ── Audience DNA ─────────────────────────────────────────────────────────

    async def audience_dna_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("audience_dna_list uid=%d", uid)
            return _err(str(exc), 500)

    # ── Auto Funnels ──────────────────────────────────────────────────────────

    async def auto_funnels_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
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
        except Exception as exc:
            log.exception("auto_funnels_list uid=%d", uid)
            return _err(str(exc), 500)

    async def auto_funnel_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("bad funnel_id", 400)
        try:
            funnel = await pool.fetchrow(
                "SELECT enabled FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid
            )
        except Exception as exc:
            log.exception("auto_funnel_toggle fetch uid=%d fid=%d", uid, fid)
            return _err(str(exc), 500)
        if not funnel:
            return _err("not found", 404)
        new_state = not funnel["enabled"]
        try:
            await pool.execute(
                "UPDATE auto_funnels SET enabled=$1, updated_at=NOW() WHERE id=$2 AND owner_id=$3", new_state, fid, uid
            )
        except Exception as exc:
            log.exception("auto_funnel_toggle update uid=%d fid=%d", uid, fid)
            return _err(str(exc), 500)
        return _json_resp({"enabled": new_state})

    async def auto_funnel_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("bad funnel_id", 400)
        try:
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
        except Exception as exc:
            log.exception("auto_funnel_detail uid=%d fid=%d", uid, fid)
            return _err(str(exc), 500)

    async def auto_funnel_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        name = (body.get("name") or "").strip()
        bot_id = body.get("bot_id")
        target_segment = (body.get("target_segment") or "all").strip()
        first_message = (body.get("first_message") or "").strip()
        if not name:
            return _err("name required")
        if not bot_id:
            return _err("bot_id required")
        try:
            bot_id = int(bot_id)
        except (TypeError, ValueError):
            return _err("invalid bot_id")
        bot_row = await pool.fetchrow(
            "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id, uid,
        )
        if not bot_row:
            return _err("bot not found", 404)
        try:
            row = await pool.fetchrow(
                "INSERT INTO auto_funnels(owner_id, name, bot_id, target_segment) VALUES($1,$2,$3,$4) RETURNING id",
                uid, name, bot_id, target_segment,
            )
            fid = row["id"]
            if first_message:
                await pool.execute(
                    "INSERT INTO auto_funnel_steps(funnel_id, step_num, delay_hours, message_text) VALUES($1,1,0,$2)",
                    fid, first_message,
                )
            return _json_resp({"ok": True, "id": fid})
        except Exception as exc:
            log.exception("auto_funnel_create uid=%d", uid)
            return _err(str(exc), 500)

    async def auto_funnel_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            fid = int(request.match_info["funnel_id"])
        except (KeyError, ValueError):
            return _err("bad funnel_id", 400)
        try:
            await pool.execute(
                "DELETE FROM auto_funnels WHERE id=$1 AND owner_id=$2", fid, uid
            )
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("auto_funnel_delete uid=%d fid=%d", uid, fid)
            return _err(str(exc), 500)

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
                    """SELECT COALESCE(label, op_type) AS action,
                              status, created_at, done_items, total_items
                       FROM operation_queue WHERE owner_id=$1
                       ORDER BY created_at DESC LIMIT 10""",
                    uid)
                def _map(r):
                    s = r["status"]
                    return {
                        "action": r["action"],
                        "status": ("completed" if s == "done" else "running" if s == "running" else "error" if s == "failed" else s),
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "detail": (f'{r["done_items"]}/{r["total_items"]}' if (r["total_items"] or 0) > 0 else None),
                    }
                return [_map(r) for r in rows]
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
    app.router.add_post("/api/miniapp/bot/add", bot_add)
    app.router.add_delete("/api/miniapp/bot/{bot_id}", bot_remove)
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
    app.router.add_post("/api/miniapp/operation/{op_id}/retry", retry_operation)
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
    app.router.add_post("/api/miniapp/crm/contact", crm_contact_create)
    app.router.add_delete("/api/miniapp/crm/contact/{contact_id}", crm_contact_delete)
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
    # Settings
    app.router.add_get("/api/miniapp/settings", user_settings_get)
    app.router.add_post("/api/miniapp/settings", user_settings_save)
    # Payments history
    app.router.add_get("/api/miniapp/payments", payments_history)
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
    app.router.add_post("/api/miniapp/api_key", create_api_key)
    app.router.add_delete("/api/miniapp/api_key/{key_id}", revoke_api_key)
    app.router.add_get("/api/miniapp/bot/{bot_id}/multigeo", multigeo_get)
    app.router.add_post("/api/miniapp/bot/{bot_id}/multigeo", multigeo_set)
    # Strike history
    app.router.add_get("/api/miniapp/strike/history", strike_history)
    app.router.add_get("/api/miniapp/strike/status", strike_status)
    app.router.add_post("/api/miniapp/strike/launch", strike_launch)
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
    app.router.add_post("/api/miniapp/experiment", experiment_create)
    app.router.add_get("/api/miniapp/experiment/{exp_id}", experiment_detail)
    app.router.add_delete("/api/miniapp/experiment/{exp_id}", experiment_delete)
    # Health Dashboard
    app.router.add_get("/api/miniapp/health", health_overview)
    # Topology Map
    app.router.add_get("/api/miniapp/topology", topology_overview)
    # Infra Analytics
    app.router.add_get("/api/miniapp/infra", infra_analytics_overview)
    # Reporter
    app.router.add_get("/api/miniapp/diag", diag)
    app.router.add_get("/api/miniapp/new_users", new_users)
    app.router.add_get("/api/miniapp/new_users/export", new_users_export)
    app.router.add_get("/api/miniapp/platform_users", platform_new_users)
    app.router.add_get("/api/miniapp/platform_users/export", platform_new_users_export)
    app.router.add_post("/api/miniapp/accounts/check", accounts_check)
    app.router.add_post("/api/miniapp/accounts/mass", accounts_mass)
    app.router.add_post("/api/miniapp/account/{acc_id}/profile", account_profile)
    app.router.add_post("/api/miniapp/channel/{ch_id}/edit", channel_edit)
    app.router.add_post("/api/miniapp/channel/{ch_id}/promote", channel_promote)
    app.router.add_post("/api/miniapp/channels/mass", channels_mass)
    app.router.add_delete("/api/miniapp/channel/{ch_id}", channel_remove)
    app.router.add_post("/api/miniapp/account/{acc_id}/toggle", account_toggle)
    app.router.add_post("/api/miniapp/account/{acc_id}/check", account_check_one)
    app.router.add_post("/api/miniapp/account/{acc_id}/action/{act}", account_action)
    app.router.add_delete("/api/miniapp/account/{acc_id}", account_delete)
    app.router.add_post("/api/miniapp/boost", boost_submit)
    app.router.add_post("/api/miniapp/growth", growth_submit)
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
    app.router.add_post("/api/miniapp/node", node_create)
    app.router.add_delete("/api/miniapp/node/{node_id}", node_delete)
    app.router.add_get("/api/miniapp/node/{node_id}/threads", node_threads)
    # Gift Transfer
    app.router.add_get("/api/miniapp/gifts", gift_inventory)
    app.router.add_post("/api/miniapp/gifts/scan", gift_scan_submit)
    # Mass Inviter
    app.router.add_post("/api/miniapp/mass_invite", mass_inviter_submit)
    # Stars Hub
    app.router.add_get("/api/miniapp/stars", stars_overview)
    app.router.add_post("/api/miniapp/stars/experiment", stars_experiment_create)
    app.router.add_put("/api/miniapp/stars/experiment/{exp_id}/toggle", stars_experiment_toggle)
    # Ghost Engine
    app.router.add_get("/api/miniapp/ghost", ghost_profiles)
    app.router.add_post("/api/miniapp/ghost", ghost_create)
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
    app.router.add_post("/api/miniapp/asset_template", create_template)
    app.router.add_get("/api/miniapp/asset_template/{tpl_id}", asset_template_detail)
    app.router.add_delete("/api/miniapp/asset_template/{tpl_id}", asset_template_delete)
    # Infra Health Center
    app.router.add_get("/api/miniapp/infra_health", infra_health_overview)
    # Swarm
    app.router.add_get("/api/miniapp/swarm", swarm_metrics)
    # Presence Packs
    app.router.add_get("/api/miniapp/presence_packs", presence_packs_list)
    app.router.add_post("/api/miniapp/presence_pack", presence_pack_create)
    app.router.add_delete("/api/miniapp/presence_pack/{pack_id}", presence_pack_delete)
    # Global Presence
    app.router.add_get("/api/miniapp/global_presence", global_presence_plans)
    app.router.add_get("/api/miniapp/global_presence/{plan_id}", global_presence_plan_detail)
    # Mass Ops
    app.router.add_get("/api/miniapp/mass_ops", mass_ops_overview)
    # Ecosystems
    app.router.add_get("/api/miniapp/ecosystems", ecosystems_list)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}", ecosystem_detail)
    app.router.add_post("/api/miniapp/ecosystem", ecosystem_create)
    app.router.add_delete("/api/miniapp/ecosystem/{eco_id}", ecosystem_delete)
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
    app.router.add_post("/api/miniapp/clone_adapt/submit", clone_adapt_submit)
    # Content Mesh
    app.router.add_get("/api/miniapp/content_meshes", content_meshes_list)
    app.router.add_post("/api/miniapp/content_mesh", content_mesh_create)
    app.router.add_put("/api/miniapp/content_mesh/{mesh_id}/toggle", content_mesh_toggle)
    # Narrative Engine
    app.router.add_get("/api/miniapp/narrative", narrative_campaigns_list)
    app.router.add_post("/api/miniapp/narrative", narrative_campaign_create)
    app.router.add_get("/api/miniapp/narrative/{campaign_id}", narrative_campaign_detail)
    # Self Promo
    app.router.add_get("/api/miniapp/self_promo", self_promo_list)
    app.router.add_post("/api/miniapp/self_promo/template", self_promo_create)
    app.router.add_delete("/api/miniapp/self_promo/{tpl_id}", self_promo_delete)
    app.router.add_put("/api/miniapp/self_promo/{tpl_id}/toggle", self_promo_toggle)
    app.router.add_post("/api/miniapp/self_promo/{tpl_id}/launch", self_promo_launch)
    # Semantic Memory
    app.router.add_get("/api/miniapp/semantic_memory", semantic_memory_overview)
    app.router.add_get("/api/miniapp/semantic_memory/{bot_id}", semantic_memory_bot)
    # Audience DNA
    app.router.add_get("/api/miniapp/audience_dna", audience_dna_list)
    # Auto Funnels
    app.router.add_get("/api/miniapp/auto_funnels", auto_funnels_list)
    app.router.add_post("/api/miniapp/auto_funnel", auto_funnel_create)
    app.router.add_put("/api/miniapp/auto_funnel/{funnel_id}/toggle", auto_funnel_toggle)
    app.router.add_get("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_detail)
    app.router.add_delete("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_delete)
    # SSE
    app.router.add_get("/api/miniapp/events", events)

    # ── Diagnostics ──────────────────────────────────────────────────────────
    async def api_health(request: web.Request) -> web.Response:
        """Публичный health endpoint для диагностики — не требует токена."""
        checks = {}
        try:
            await pool.fetchval("SELECT 1")
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {e}"
        try:
            n_accounts = await pool.fetchval("SELECT COUNT(*) FROM tg_accounts")
            n_ops = await pool.fetchval("SELECT COUNT(*) FROM operation_queue WHERE status IN ('pending','running')")
            checks["accounts_total"] = int(n_accounts or 0)
            checks["ops_active"] = int(n_ops or 0)
        except Exception as e:
            checks["stats"] = f"error: {e}"
        checks["routes"] = len([r for r in request.app.router.routes()])
        checks["status"] = "ok" if checks.get("db") == "ok" else "degraded"
        return _json_resp(checks)
    app.router.add_get("/api/miniapp/sys_health", api_health)

    async def miniapp_config(request: web.Request) -> web.Response:
        """Public config endpoint — no auth required. Returns bot info for frontend."""
        bot_username = await _resolve_bot_username()
        mini_app_url = os.getenv("MINI_APP_URL", "")
        try:
            from config import PLAN_PRICES_USD, PERIOD_DISCOUNTS
            paid_price = int(PLAN_PRICES_USD.get("paid", 29))
            period_discounts = {str(k): v for k, v in PERIOD_DISCOUNTS.items()}
        except Exception:
            paid_price = 29
            period_discounts = {"1": 0, "3": 10, "6": 15, "12": 20}
        return _json_resp({
            "bot_username": bot_username,
            "mini_app_url": mini_app_url,
            "platform": "Infragram OS",
            "version": "2.0",
            "paid_price": paid_price,
            "period_discounts": period_discounts,
        })

    app.router.add_get("/api/miniapp/config", miniapp_config)

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
