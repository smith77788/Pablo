"""Mini App API — aiohttp routes for the Telegram Mini App + SSE real-time updates."""
from __future__ import annotations

import asyncio
import json
import json as _json  # модульный алиас: ряд эндпоинтов используют _json без локального import
import logging
import os
import time
from typing import Any

import asyncpg
from aiohttp import web

from services.mini_app_auth import validate_init_data, make_token, parse_token
from services.security import (
    check_rate_limit,
    rate_limit_response,
    validate_integer,
    validate_string,
    sanitize_search_query,
    check_sql_suspicious,
    escape_html,
    escape_json_value,
    security_middleware,
)

log = logging.getLogger(__name__)

# CRM-статусы жизненного цикла аккаунта (ручная воронка оператора, аналог
# «ПЕРЕМЕСТИТЬ В СТАТУС» Telegram Expert). Отдельно от acc_status (техздоровье).
# Ключ хранится в tg_accounts.stage; подпись/эмодзи — на клиенте.
ACCOUNT_STAGES = {"new", "warming", "ready", "in_work", "resting", "frozen", "reserve"}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 30  # секунд

def _cached(key: str, ttl: int = _CACHE_TTL):
    """Декоратор для кэширования результатов."""
    def wrapper(fn):
        async def wrapped(*args, **kwargs):
            now = time.time()
            if key in _cache and now - _cache[key][0] < ttl:
                return _cache[key][1]
            result = await fn(*args, **kwargs)
            _cache[key] = (now, result)
            return result
        return wrapped
    return wrapper

def _cached_user(ttl: int = _CACHE_TTL):
    """Декоратор для кэширования результатов по user ID."""
    def wrapper(fn):
        async def wrapped(*args, **kwargs):
            request = args[0] if args else kwargs.get('request')
            uid = None
            if request:
                uid = _get_uid(request)
            if uid:
                key = f"{fn.__name__}:{uid}"
                now = time.time()
                if key in _cache and now - _cache[key][0] < ttl:
                    return _cache[key][1]
                result = await fn(*args, **kwargs)
                _cache[key] = (now, result)
                return result
            return await fn(*args, **kwargs)
        return wrapped
    return wrapper


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
            except Exception as e:
                log.warning("_resolve_bot_username session.close: %s", e)
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


# Единый маппинг UI-операции редактирования канала → op воркера bulk_chan_exec.
# Используется и в channel_edit (одиночный), и в channels_mass (массовый) —
# один источник истины, чтобы наборы не разъехались.
_CHANNEL_EDIT_OPS = {"title": "chan_title", "about": "chan_about", "username": "chan_uname"}


def channel_edit_worker_op(op) -> str | None:
    """UI-op ('title'|'about'|'username') → воркер-op, либо None если не поддержан."""
    return _CHANNEL_EDIT_OPS.get(op)


def is_safe_public_url(url: str) -> bool:
    """SSRF-гард для загрузки картинок по URL (аватар бота).

    Требует https, отсекает localhost и приватные диапазоны IP в hostname.
    Best-effort (без резолва DNS): блокирует очевидные внутренние адреса.
    Чистая функция — тестируема.
    """
    import re as _re
    from urllib.parse import urlparse
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "0.0.0.0") or host.endswith(".local") or host.endswith(".internal"):
        return False
    # Приватные / loopback / link-local диапазоны по literal-IP в hostname.
    if _re.match(r"^127\.", host) or _re.match(r"^10\.", host) \
       or _re.match(r"^192\.168\.", host) or _re.match(r"^169\.254\.", host) \
       or _re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host) or host == "::1":
        return False
    return True


_SCHEDULE_REPEAT_MIN = {"none": 0, "daily": 1440, "weekly": 10080}


def schedule_repeat_minutes(kind) -> int:
    """Период повтора расписания в минутах. none/неизвестное → 0 (одноразовое).
    daily → 1440, weekly → 10080. Чистая функция — тестируема."""
    return _SCHEDULE_REPEAT_MIN.get(str(kind or "none").lower(), 0)


def parse_proxy_type(proxy_url: str) -> str | None:
    """Определяет тип прокси по схеме URL. None — если схема не поддержана.

    Единый источник истины для add_proxy и import_proxies. Поддержка:
    socks5://, socks4://, http://. Чистая функция — тестируема.
    """
    u = (proxy_url or "").strip().lower()
    if u.startswith("socks5://"):
        return "socks5"
    if u.startswith("socks4://"):
        return "socks4"
    if u.startswith("http://"):
        return "http"
    return None


def parsed_audience_filters(q, base_params_count: int = 1):
    """Строит доп. условия WHERE + параметры для выборки parsed_audiences.

    q: mapping с ключами source, premium, with_username, not_bot, active, with_phone.
    Плейсхолдеры продолжаются с base_params_count (после owner_id=$1). Богатые
    колонки (is_premium/is_bot/is_active/phone) раньше хранились, но не фильтровались.
    Чистая функция — тестируема.
    """
    def _truthy(v) -> bool:
        return str(v).lower() in ("1", "true", "yes", "on")
    conds: list[str] = []
    params: list = []
    idx = base_params_count
    src = (q.get("source") or "").strip()
    if src:
        idx += 1
        conds.append(f"source_username ILIKE ${idx}")
        params.append(f"%{src}%")
    if _truthy(q.get("premium")):
        conds.append("is_premium=TRUE")
    if _truthy(q.get("with_username")):
        conds.append("username IS NOT NULL AND username<>''")
    if _truthy(q.get("not_bot")):
        conds.append("COALESCE(is_bot,FALSE)=FALSE")
    if _truthy(q.get("active")):
        conds.append("is_active=TRUE")
    if _truthy(q.get("with_phone")):
        conds.append("phone IS NOT NULL AND phone<>''")
    sql = (" AND " + " AND ".join(conds)) if conds else ""
    return sql, params


def _spintax_pack(templates: list[str], random_sample: bool = False) -> list[dict[str, Any]]:
    """Список шаблонов → элементы для фронта: шаблон, пример, предупреждения.

    По умолчанию пример — «скелет» (первые варианты групп = исходный текст), он
    всегда читается чисто. Случайную комбинацию отдаём только при явном запросе
    раскрутки (``random_sample=True``).
    """
    from services import spintax_service

    items: list[dict[str, Any]] = []
    for tpl in templates:
        try:
            if random_sample:
                sample = spintax_service.expand_template(tpl)
            else:
                sample = spintax_service.first_option_render(tpl)
        except Exception:
            sample = ""
        items.append(
            {
                "template": tpl,
                "sample": sample,
                "warnings": spintax_service.quality_warnings(tpl),
            }
        )
    return items


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
    if not uid:
        return False
    # Check permanent admin IDs from environment
    if uid in _admin_ids():
        return True
    # Check session admins (authenticated via ADMIN_SECRET in bot)
    try:
        from bot.handlers.admin import _session_admins
        if uid in _session_admins:
            return True
    except Exception as e:
        log.warning("_is_admin import session_admins: %s", e)
    return False


def _jlist(val) -> list:
    """Safe JSONB→list: asyncpg may return already-parsed list or a JSON string."""
    if isinstance(val, list):
        return val
    if val is None:
        return []
    try:
        return json.loads(val) or []
    except Exception:
        return []


def _csv_resp(filename: str, header: list[str], rows: list[list]) -> web.Response:
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    buf.write("﻿")  # BOM для корректного Excel UTF-8
    w = _csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    import re as _re
    safe_name = _re.sub(r'[^a-zA-Z0-9_\-.]', '_', filename)
    return web.Response(
        text=buf.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
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


def _dna_to_dict(dna: Any) -> dict:
    """Serialize an AudienceDNA dataclass instance to a JSON-friendly dict."""
    return {
        "bot_id": dna.bot_id,
        "owner_id": dna.owner_id,
        "peak_hours": list(dna.peak_hours),
        "peak_days": list(dna.peak_days),
        "best_content_types": list(dna.best_content_types),
        "avg_engagement_rate": dna.avg_engagement_rate,
        "churn_risk_pct": dna.churn_risk_pct,
        "top_topics": list(dna.top_topics),
        "total_users_analyzed": dna.total_users_analyzed,
        "computed_at": dna.computed_at.isoformat() if dna.computed_at else None,
    }


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
    # Apply security middleware (rate limiting + security headers)
    app.middlewares.append(security_middleware())

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
            # Automation Workflows
            """CREATE TABLE IF NOT EXISTS automation_workflows (
                id BIGSERIAL PRIMARY KEY,
                owner_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                steps JSONB NOT NULL DEFAULT '[]',
                status TEXT DEFAULT 'created',
                created_at TIMESTAMPTZ DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_wf_owner ON automation_workflows(owner_id, created_at DESC)",
            """CREATE TABLE IF NOT EXISTS workflow_step_runs (
                id BIGSERIAL PRIMARY KEY,
                workflow_id BIGINT NOT NULL REFERENCES automation_workflows(id) ON DELETE CASCADE,
                step_num INTEGER NOT NULL,
                action TEXT NOT NULL DEFAULT '',
                params JSONB DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                label TEXT DEFAULT '',
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                result_data JSONB DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_wf_steps_wf ON workflow_step_runs(workflow_id, step_num)",
            """CREATE TABLE IF NOT EXISTS workflow_step_logs (
                id BIGSERIAL PRIMARY KEY,
                workflow_id BIGINT NOT NULL,
                step_num INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                message TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS idx_wf_logs_wf ON workflow_step_logs(workflow_id, created_at DESC)",
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
        init_data = validate_string(body.get("initData"), max_len=8192)
        if not init_data:
            return _err("Missing initData")
        bot_token = _bot_token()
        user = validate_init_data(init_data, bot_token)
        if not user:
            return _err("Invalid Telegram initData", 401)
        token = make_token(user["user_id"], bot_token)
        return _json_resp({"token": token, "user": user})

    # ── Dashboard ────────────────────────────────────────────────────────────

    @_cached_user()
    async def dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        # Все запросы дашборда независимы — выполняем их параллельно одним
        # gather вместо ~8 последовательных round-trip'ов к БД.
        async def _plan():
            try:
                from bot.utils.subscription import get_plan as _gp
                return await _gp(pool, uid)
            except Exception:
                return None
        _q = {
            "stats": _stats(pool, uid),
            "plan": _plan(),
            "plan_row": pool.fetchrow(
                "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid),
            "exp_row": pool.fetchrow(
                "SELECT expires_at FROM subscriptions WHERE user_id=$1 AND is_active=true "
                "AND expires_at > now() ORDER BY expires_at DESC LIMIT 1", uid),
            "activity": pool.fetch(
                """SELECT COALESCE(label, op_type) AS action, status, created_at,
                          done_items, total_items, error_msg
                   FROM operation_queue WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 10""", uid),
            "acc_health": pool.fetchval(
                """SELECT ROUND(AVG(
                    CASE WHEN COALESCE(trust_score, 1.0) < 0.1 THEN 0.5
                         ELSE COALESCE(trust_score, 1.0)
                    END
                ) * 100) FROM tg_accounts WHERE owner_id=$1 AND is_active=true""", uid),
            "queue_backlog": pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'", uid),
            "ops_failed": pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='failed' AND created_at > NOW() - INTERVAL '24 hours'", uid),
        }
        _keys = list(_q.keys())
        _res = await asyncio.gather(*_q.values(), return_exceptions=True)
        r = {k: (None if isinstance(v, Exception) else v) for k, v in zip(_keys, _res)}

        stats = r["stats"] or {"bots": 0, "channels": 0, "subscribers": 0,
                               "campaigns_active": 0, "funnels_active": 0,
                               "accounts": 0, "ops_running": 0}
        # План
        plan_row = r["plan_row"]
        try:
            stats["plan"] = r["plan"]
            if not stats.get("plan"):
                stats["plan"] = (plan_row["current_plan"] if plan_row else "free") or "free"
            try:
                from bot.utils.subscription import coerce_plan as _cp
                stats["plan"] = _cp(stats["plan"])
            except Exception as e:
                log.warning("dashboard coerce_plan: %s", e)
            if r["exp_row"]:
                stats["plan_expires_at"] = str(r["exp_row"]["expires_at"])
            else:
                stats["plan_expires_at"] = str(plan_row["plan_expires_at"]) if plan_row and plan_row["plan_expires_at"] else None
        except Exception:
            stats["plan"] = "free"
            stats["plan_expires_at"] = None
        # Лента активности
        def _op_action(s):
            return ("completed" if s == "done" else
                    "running" if s == "running" else
                    "error" if s == "failed" else s)
        try:
            stats["recent_activity"] = [
                {
                    "action": a["action"],
                    "status": _op_action(a["status"]),
                    "created_at": a["created_at"].isoformat() if a["created_at"] else None,
                    "detail": (f'{a["done_items"]}/{a["total_items"]}' if (a["total_items"] or 0) > 0 else None),
                }
                for a in (r["activity"] or [])
            ]
        except Exception:
            stats["recent_activity"] = []
        stats["acc_health"] = int(r["acc_health"]) if r["acc_health"] is not None else 100
        stats["queue_backlog"] = int(r["queue_backlog"] or 0)
        stats["ops_failed"] = int(r["ops_failed"] or 0)
        return _json_resp(stats)

    # ── Bots ─────────────────────────────────────────────────────────────────

    @_cached_user()
    async def bots(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            limit = min(validate_integer(request.rel_url.query.get("limit", "500"), min_val=1, max_val=5000) or 500, 5000)
        except (ValueError, TypeError):
            limit = 500
        # Показываем ВСЕ боты доступные пользователю:
        # 1) Личные боты (added_by = uid)
        # 2) Боты из экосистем пользователя (ecosystem_members)
        # 3) Боты из рабочих пространств пользователя (workspace_members)
        rows = await _safe_fetch(pool,
            """SELECT DISTINCT mb.bot_id, mb.username, mb.first_name, mb.is_active,
                      COUNT(DISTINCT bu.user_id) FILTER (WHERE bu.is_active=true) AS subscriber_count,
                      COUNT(DISTINCT bu.user_id) AS total_users
               FROM managed_bots mb
               LEFT JOIN bot_users bu ON bu.bot_id=mb.bot_id
               WHERE mb.added_by=$1
                  OR mb.bot_id IN (
                      SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                      JOIN ecosystems e ON e.id=eb.ecosystem_id
                      WHERE e.owner_id=$1
                         OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
                  )
                  OR mb.bot_id IN (
                      SELECT DISTINCT b.bot_id FROM managed_bots b
                      JOIN workspaces w ON w.owner_id=b.added_by
                      JOIN workspace_members wm ON wm.workspace_id=w.id
                      WHERE wm.user_id=$1
                  )
               GROUP BY mb.bot_id, mb.username, mb.first_name, mb.is_active
               ORDER BY subscriber_count DESC LIMIT $2""", uid, limit)
        total = await _safe_count(pool,
            """SELECT COUNT(DISTINCT bot_id) FROM managed_bots
               WHERE added_by=$1
                  OR bot_id IN (
                      SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                      JOIN ecosystems e ON e.id=eb.ecosystem_id
                      WHERE e.owner_id=$1
                         OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
                  )
                  OR bot_id IN (
                      SELECT DISTINCT b.bot_id FROM managed_bots b
                      JOIN workspaces w ON w.owner_id=b.added_by
                      JOIN workspace_members wm ON wm.workspace_id=w.id
                      WHERE wm.user_id=$1
                  )""", uid)
        return _json_resp({"bots": rows, "total": int(total or 0)})

    async def bot_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        # Проверяем доступ: личный бот ИЛИ бот из экосистемы/рабочего пространства
        owns = await _safe_count(pool,
            """SELECT COUNT(*) FROM managed_bots mb
               WHERE mb.bot_id=$1 AND (
                   mb.added_by=$2
                   OR mb.bot_id IN (
                       SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                       JOIN ecosystems e ON e.id=eb.ecosystem_id
                       WHERE e.owner_id=$2
                          OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$2)
                   )
                   OR mb.bot_id IN (
                       SELECT DISTINCT b.bot_id FROM managed_bots b
                       JOIN workspaces w ON w.owner_id=b.added_by
                       JOIN workspace_members wm ON wm.workspace_id=w.id
                       WHERE wm.user_id=$2
                   )
               )""", bot_id, uid)
        if not owns:
            return _err("Bot not found or access denied", 404)
        bot = await _safe_fetchrow(pool,
            "SELECT * FROM managed_bots WHERE bot_id=$1", bot_id)
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
            """SELECT COUNT(*) FROM managed_bots mb
               WHERE mb.bot_id=$1 AND (
                   mb.added_by=$2
                   OR mb.bot_id IN (
                       SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                       JOIN ecosystems e ON e.id=eb.ecosystem_id
                       WHERE e.owner_id=$2
                          OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$2)
                   )
                   OR mb.bot_id IN (
                       SELECT DISTINCT b.bot_id FROM managed_bots b
                       JOIN workspaces w ON w.owner_id=b.added_by
                       JOIN workspace_members wm ON wm.workspace_id=w.id
                       WHERE wm.user_id=$2
                   )
               )""", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        # Счётчик срабатываний из auto_reply_log — аналитика прямо в списке.
        # LEFT JOIN, чтобы правила без срабатываний тоже вернулись (fired=0).
        # Фолбэк на простой SELECT, если новых колонок/лога ещё нет.
        try:
            rows = await pool.fetch(
                """SELECT ar.id, ar.trigger_type, ar.keyword, ar.response_text,
                          ar.is_active, ar.created_at, ar.match_mode,
                          ar.reply_delay_sec, ar.active_from_hour, ar.active_to_hour, ar.priority,
                          COALESCE(l.fired, 0) AS fired
                   FROM auto_replies ar
                   LEFT JOIN (
                       SELECT rule_id, COUNT(*) AS fired FROM auto_reply_log
                       WHERE bot_id=$1 AND rule_type='auto_reply' GROUP BY rule_id
                   ) l ON l.rule_id = ar.id
                   WHERE ar.bot_id=$1
                   ORDER BY ar.priority DESC, ar.created_at DESC""",
                bot_id)
            rows = [dict(r) for r in rows]
        except Exception:
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
            """SELECT COUNT(*) FROM managed_bots mb
               WHERE mb.bot_id=$1 AND (
                   mb.added_by=$2
                   OR mb.bot_id IN (
                       SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                       JOIN ecosystems e ON e.id=eb.ecosystem_id
                       WHERE e.owner_id=$2
                          OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$2)
                   )
                   OR mb.bot_id IN (
                       SELECT DISTINCT b.bot_id FROM managed_bots b
                       JOIN workspaces w ON w.owner_id=b.added_by
                       JOIN workspace_members wm ON wm.workspace_id=w.id
                       WHERE wm.user_id=$2
                   )
                )""", bot_id, uid)
        if not owns:
            return _err("Bot not found", 404)
        trigger_type = body.get("trigger_type", "keyword")
        keyword = body.get("keyword", "").strip()
        response_text = body.get("response_text", "").strip()
        if not response_text:
            return _err("response_text required", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO auto_replies (bot_id, trigger_type, keyword, response_text, is_active)
                   VALUES ($1,$2,$3,$4,TRUE) RETURNING id""",
                bot_id, trigger_type, keyword or None, response_text)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as e:
            log.warning("create_auto_reply bot=%d: %s", bot_id, e)
            return _err("Failed to create auto reply", 500)

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
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        text = validate_string(body.get("text"), max_len=4096)
        if not bot_id or not text:
            return _err("bot_id and text required")
        # SQL injection defense on text
        if check_sql_suspicious(text):
            return _err("Invalid characters in message text")
        if len(text) > 4096:
            return _err("Message too long (max 4096 chars)")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id")
        silent = bool(body.get("silent"))
        # Сегмент аудитории: all | active_7d | active_30d
        segment = validate_string(body.get("segment") or "all", max_len=20) or "all"
        _seg_sql = {
            "active_7d": " AND last_seen >= now() - interval '7 days'",
            "active_30d": " AND last_seen >= now() - interval '30 days'",
        }.get(segment, "")
        # Инлайн-кнопки (необязательно): [{text, url}] — валидируем и ограничиваем.
        buttons = []
        for b in (body.get("buttons") or [])[:10]:
            try:
                bt = validate_string(b.get("text"), max_len=64) or ""
                bu = validate_string(b.get("url"), max_len=2048) or ""
            except Exception:
                continue
            if bt and bu.lower().startswith(("http://", "https://")):
                buttons.append({"text": escape_html(bt), "url": bu})
        bot_row = await _safe_fetchrow(pool,
            "SELECT bot_id, token, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id_int, uid)
        if not bot_row:
            return _err("Bot not found", 404)
        total = await _safe_count(pool,
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=true" + _seg_sql, bot_id_int)
        # Отложенная отправка: schedule_minutes минут от текущего момента.
        try:
            schedule_minutes = max(0, min(int(body.get("schedule_minutes") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            schedule_minutes = 0
        try:
            bot_label = bot_row.get("username") or bot_id_int
            label = f"Рассылка боту @{bot_label}: {text[:40]}…" if len(text) > 40 else f"Рассылка: {text[:60]}"
            _op_params = {"bot_id": bot_id_int, "text": text}
            if buttons:
                _op_params["buttons"] = buttons
            if silent:
                _op_params["silent"] = True
            if _seg_sql:
                _op_params["segment"] = segment
            # Autopost v2: рекуррентная рассылка (0 = разовая). Движок op_worker
            # сам переочередит следующий запуск через repeat_interval_min минут.
            try:
                _rmin = max(0, min(int(body.get("repeat_interval_min") or 0), 60 * 24 * 30))
            except (TypeError, ValueError):
                _rmin = 0
            if _rmin > 0:
                _op_params["repeat_interval_min"] = _rmin
                _rc = body.get("repeat_count")
                if _rc is not None:
                    try:
                        _op_params["repeat_count"] = max(1, min(int(_rc), 1000))
                    except (TypeError, ValueError):
                        pass

            broadcast_id = None
            if schedule_minutes <= 0:
                # Немедленная отправка: создаём запись рассылки сразу (прогресс/resume).
                try:
                    row = await pool.fetchrow(
                        "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by, buttons, silent) "
                        "VALUES($1,$2,$3,'pending',$4,$5::jsonb,$6) RETURNING id",
                        bot_id_int, text, total, uid, _json.dumps(buttons) if buttons else None, silent)
                except Exception:
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
                _op_params["broadcast_id"] = broadcast_id
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
                    uid, _json.dumps(_op_params), total, label)
            else:
                # Отложенная: НЕ создаём запись рассылки заранее (иначе resume
                # отправит её сразу при рестарте). Воркер создаст её, когда
                # наступит scheduled_for. Планировщик воркера уважает scheduled_for.
                label = f"⏰ {label}"
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label, scheduled_for) "
                    "VALUES($1,'run_broadcast','pending',$2,$3,$4, now() + ($5 || ' minutes')::interval) RETURNING id",
                    uid, _json.dumps(_op_params), total, label, str(schedule_minutes))
            return _json_resp({"ok": True, "broadcast_id": broadcast_id, "op_id": op_id,
                               "total_users": total, "scheduled_minutes": schedule_minutes})
        except Exception:
            log.exception("create_broadcast bot=%d uid=%d", bot_id_int, uid)
            return _err("Failed to create broadcast", 500)

    async def broadcast_resend(request: web.Request) -> web.Response:
        """Повторная отправка рассылки только НЕдоставленным получателям."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bc_id = int(request.match_info["bc_id"])
        except (KeyError, ValueError):
            return _err("bad broadcast id", 400)
        # Исходная рассылка должна принадлежать пользователю (created_by).
        src = await _safe_fetchrow(pool,
            "SELECT id, bot_id, message_text, created_by FROM broadcasts WHERE id=$1", bc_id)
        if not src or int(src.get("created_by") or 0) != uid:
            return _err("Рассылка не найдена", 404)
        bot_id_int = int(src["bot_id"])
        # Владение ботом (double-check).
        bot_row = await _safe_fetchrow(pool,
            "SELECT bot_id, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id_int, uid)
        if not bot_row:
            return _err("Бот не найден", 404)
        text = (src.get("message_text") or "").strip()
        if not text:
            return _err("У исходной рассылки нет текста", 400)
        # Недоставленные = активные подписчики, которых НЕТ в delivery_log исходной.
        rows = await _safe_fetch(pool,
            """SELECT bu.user_id FROM bot_users bu
               WHERE bu.bot_id=$1 AND bu.is_active=true
                 AND NOT EXISTS (
                     SELECT 1 FROM broadcast_delivery_log dl
                     WHERE dl.broadcast_id=$2 AND dl.user_id=bu.user_id)""",
            bot_id_int, bc_id)
        undelivered = [int(r["user_id"]) for r in (rows or [])]
        if not undelivered:
            return _err("Все активные подписчики уже получили рассылку", 400)
        total = len(undelivered)
        try:
            row = await pool.fetchrow(
                "INSERT INTO broadcasts(bot_id, message_text, total_users, status, created_by) "
                "VALUES($1,$2,$3,'pending',$4) RETURNING id",
                bot_id_int, text, total, uid)
            new_bc = row["id"]
            label = f"Повтор недоставленным: {text[:40]}…" if len(text) > 40 else f"Повтор: {text[:60]}"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'run_broadcast','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"bot_id": bot_id_int, "broadcast_id": new_bc,
                                  "text": text, "user_ids": undelivered}),
                total, label)
            return _json_resp({"ok": True, "broadcast_id": new_bc, "op_id": op_id, "total_users": total})
        except Exception:
            log.exception("broadcast_resend bc=%d uid=%d", bc_id, uid)
            return _err("Не удалось создать повторную рассылку", 500)

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

    async def broadcast_schedule(request: web.Request) -> web.Response:
        """Рассылка с расписанием: POST /api/miniapp/broadcast/schedule"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        text = validate_string(body.get("text"), max_len=4096)
        if not bot_id or not text:
            return _err("bot_id and text required")
        if check_sql_suspicious(text):
            return _err("Invalid characters in message text")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id")
        schedule = {
            "schedule_minutes": body.get("schedule_minutes"),
            "scheduled_for": body.get("scheduled_for"),
            "segment": body.get("segment"),
            "buttons": body.get("buttons"),
        }
        try:
            from services.broadcaster import mass_broadcast_with_scheduling
            result = await mass_broadcast_with_scheduling(pool, uid, bot_id_int, text, schedule)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error", "Failed"), 400)
        except Exception:
            log.exception("broadcast_schedule uid=%d bot=%d", uid, bot_id_int)
            return _err("Failed to create scheduled broadcast", 500)

    async def broadcast_ab_test(request: web.Request) -> web.Response:
        """A/B тестирование рассылок: POST /api/miniapp/broadcast/ab_test"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        bot_id = validate_integer(body.get("bot_id"), min_val=1)
        variants = body.get("variants")
        if not bot_id or not variants:
            return _err("bot_id and variants required")
        try:
            bot_id_int = int(bot_id)
        except (TypeError, ValueError):
            return _err("Invalid bot_id")
        if not isinstance(variants, list) or len(variants) < 2:
            return _err("variants must be a list with at least 2 items")
        for v in variants:
            if not isinstance(v, dict) or not v.get("text"):
                return _err("Each variant must have 'text'")
        try:
            from services.broadcaster import ab_test_broadcast
            result = await ab_test_broadcast(pool, uid, bot_id_int, variants)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error", "Failed"), 400)
        except Exception:
            log.exception("broadcast_ab_test uid=%d bot=%d", uid, bot_id_int)
            return _err("Failed to create A/B test broadcast", 500)

    async def broadcast_analytics(request: web.Request) -> web.Response:
        """Аналитика рассылки: GET /api/miniapp/broadcast/{id}/analytics"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bc_id = int(request.match_info["bc_id"])
        except (KeyError, ValueError):
            return _err("Invalid broadcast id", 400)
        try:
            from services.broadcaster import get_broadcast_analytics
            result = await get_broadcast_analytics(pool, uid, bc_id)
            if result.get("ok"):
                return _json_resp(result)
            return _err(result.get("error", "Not found"), 404)
        except Exception:
            log.exception("broadcast_analytics uid=%d bc=%d", uid, bc_id)
            return _err("Failed to get analytics", 500)

    # ── Channels ─────────────────────────────────────────────────────────────

    @_cached_user()
    async def channels(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            limit = min(validate_integer(request.rel_url.query.get("limit", "500"), min_val=1, max_val=5000) or 500, 5000)
        except (ValueError, TypeError):
            limit = 500
        try:
            offset = max(validate_integer(request.rel_url.query.get("offset", "0"), min_val=0) or 0, 0)
        except (ValueError, TypeError):
            offset = 0
        # Показываем ВСЕ каналы доступные пользователю:
        # 1) Личные каналы (owner_id = uid)
        # 2) Каналы из экосистем пользователя
        # 3) Каналы из рабочих пространств пользователя
        # 4) Каналы созданные через подключённые аккаунты
        rows = await _safe_fetch(pool,
            """SELECT DISTINCT channel_id AS id, channel_id, username, title,
                      COALESCE(members_count, 0) AS member_count,
                      type, added_at
               FROM managed_channels
               WHERE owner_id=$1
                  OR id IN (
                      SELECT DISTINCT ec.channel_id FROM ecosystem_channels ec
                      JOIN ecosystems e ON e.id=ec.ecosystem_id
                      WHERE e.owner_id=$1
                         OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
                  )
                  OR id IN (
                      SELECT DISTINCT mc.id FROM managed_channels mc
                      JOIN workspaces w ON w.owner_id=mc.owner_id
                      JOIN workspace_members wm ON wm.workspace_id=w.id
                      WHERE wm.user_id=$1
                  )
               ORDER BY members_count DESC NULLS LAST
               LIMIT $2 OFFSET $3""", uid, limit, offset)
        total = await _safe_count(pool,
            """SELECT COUNT(DISTINCT id) FROM managed_channels
               WHERE owner_id=$1
                  OR id IN (
                      SELECT DISTINCT ec.channel_id FROM ecosystem_channels ec
                      JOIN ecosystems e ON e.id=ec.ecosystem_id
                      WHERE e.owner_id=$1
                         OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$1)
                  )
                  OR id IN (
                      SELECT DISTINCT mc.id FROM managed_channels mc
                      JOIN workspaces w ON w.owner_id=mc.owner_id
                      JOIN workspace_members wm ON wm.workspace_id=w.id
                      WHERE wm.user_id=$1
                  )""", uid)
        return _json_resp({"channels": rows, "total": int(total or 0), "offset": offset, "limit": limit})

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
        except Exception as e:
            log.warning("funnels_all uid=%d: %s", uid, e)
            return _json_resp({"funnels": []})

    # ── Accounts ─────────────────────────────────────────────────────────────

    def _accounts_where(uid: int, flt: str, stage: str, q: str, admin: bool = False) -> tuple[str, list]:
        """Собрать WHERE + args для списка аккаунтов из фильтра здоровья, CRM-статуса
        и поиска. Возвращает (sql_where, args). Серверная фильтрация снимает
        100-лимит клиента: срез считается по ВСЕЙ таблице, не по загруженной странице.
        admin=True — межтенантный просмотр (все аккаунты платформы, без owner-скоупа)."""
        if admin:
            clauses = ["TRUE"]
            args: list = []
        else:
            clauses = ["owner_id=$1"]
            args = [uid]
        if flt == "active":
            clauses.append("is_active AND COALESCE(acc_status,'ok') <> 'banned' "
                           "AND (cooldown_until IS NULL OR cooldown_until <= now())")
        elif flt == "cooldown":
            clauses.append("cooldown_until IS NOT NULL AND cooldown_until > now()")
        elif flt == "banned":
            clauses.append("COALESCE(acc_status,'ok') = 'banned'")
        if stage in ACCOUNT_STAGES:
            args.append(stage)
            clauses.append(f"stage = ${len(args)}")
        if q:
            args.append(f"%{q}%")
            n = len(args)
            clauses.append(f"(phone ILIKE ${n} OR first_name ILIKE ${n} OR username ILIKE ${n})")
        return " AND ".join(clauses), args

    async def accounts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        admin = _is_admin(uid)
        # Параметры серверной пагинации/фильтрации
        qs = request.rel_url.query
        flt = qs.get("filter", "all")
        if flt not in ("all", "active", "cooldown", "banned"):
            flt = "all"
        stage = (qs.get("stage") or "").strip().lower()
        if stage and stage not in ACCOUNT_STAGES:
            stage = ""
        q = (qs.get("q") or "").strip()[:64]
        try:
            offset = max(0, int(qs.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = min(200, max(1, int(qs.get("limit", 100))))
        except (TypeError, ValueError):
            limit = 100

        # admin=True — межтенантный просмотр всех аккаунтов платформы (без owner-скоупа).
        where, args = _accounts_where(uid, flt, stage, q, admin=admin)
        page_args = args + [limit, offset]
        rows = await _safe_fetch(pool,
            f"""SELECT id, phone, first_name, username, is_active, last_used, added_at,
                      COALESCE(trust_score, 100) AS trust_score,
                      COALESCE(acc_status, 'ok') AS acc_status,
                      cooldown_until, cluster, stage
               FROM tg_accounts WHERE {where}
               ORDER BY is_active DESC, last_used DESC NULLS LAST
               LIMIT ${len(args)+1} OFFSET ${len(args)+2}""", *page_args)
        # Сколько всего под текущим фильтром — для пагинации «Загрузить ещё».
        filtered_total = await _safe_count(pool,
            f"SELECT COUNT(*) FROM tg_accounts WHERE {where}", *args)
        filtered_total = int(filtered_total or 0)

        # Серверная агрегация KPI (глобальные счётчики, не срез). Админ — по всей
        # платформе, обычный пользователь — по своим аккаунтам.
        if admin:
            st = await _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok')='banned') AS banned,
                          COUNT(*) FILTER (WHERE cooldown_until IS NOT NULL AND cooldown_until > now()) AS cooldown,
                          COUNT(*) FILTER (
                              WHERE is_active
                                AND COALESCE(acc_status,'ok') <> 'banned'
                                AND (cooldown_until IS NULL OR cooldown_until <= now())
                          ) AS active
                   FROM tg_accounts""")
        else:
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
        # by_stage скоупим по owner_id для не-админа (иначе — межтенантная утечка
        # разбивки стадий по ВСЕЙ платформе). Админ видит всё, как в основной статистике.
        if admin:
            stage_rows = await _safe_fetch(pool,
                "SELECT stage, COUNT(*) AS c FROM tg_accounts "
                "WHERE stage IS NOT NULL GROUP BY stage")
        else:
            stage_rows = await _safe_fetch(pool,
                "SELECT stage, COUNT(*) AS c FROM tg_accounts "
                "WHERE owner_id=$1 AND stage IS NOT NULL GROUP BY stage", uid)
        by_stage = {r["stage"]: int(r["c"] or 0) for r in (stage_rows or [])
                    if r.get("stage") in ACCOUNT_STAGES}
        stats["by_stage"] = by_stage
        return _json_resp({
            "accounts": rows,
            "stats": stats,
            "admin_view": admin,
            "page": {
                "offset": offset, "limit": limit,
                "filtered_total": filtered_total,
                "has_more": offset + len(rows) < filtered_total,
                "filter": flt, "stage": stage, "q": q,
            },
        })

    async def account_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        admin = _is_admin(uid)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("Invalid acc_id", 400)
        if admin:
            acc = await _safe_fetchrow(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          COALESCE(trust_score, 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until, cluster, stage
                   FROM tg_accounts WHERE id=$1""", acc_id)
        else:
            acc = await _safe_fetchrow(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          COALESCE(trust_score, 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until, cluster, stage
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

    async def accounts_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        admin = _is_admin(uid)
        if admin:
            rows = await _safe_fetch(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          COALESCE(trust_score, 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until
                   FROM tg_accounts
                   ORDER BY is_active DESC, last_used DESC NULLS LAST""")
        else:
            rows = await _safe_fetch(pool,
                """SELECT id, phone, first_name, username, tg_user_id,
                          is_active, added_at, last_used,
                          COALESCE(trust_score, 100) AS trust_score,
                          COALESCE(acc_status, 'ok') AS acc_status,
                          cooldown_until
                   FROM tg_accounts WHERE owner_id=$1
                   ORDER BY is_active DESC, last_used DESC NULLS LAST""", uid)
        header = ["id", "phone", "first_name", "username", "tg_user_id",
                  "is_active", "trust_score", "acc_status", "cooldown_until",
                  "last_used", "added_at"]
        csv_rows = []
        for r in rows:
            cd = r.get("cooldown_until")
            csv_rows.append([
                r.get("id"),
                r.get("phone"),
                r.get("first_name"),
                r.get("username"),
                r.get("tg_user_id"),
                r.get("is_active"),
                r.get("trust_score"),
                r.get("acc_status"),
                cd.isoformat() if cd else "",
                r.get("last_used"),
                r.get("added_at"),
            ])
        return _csv_resp("accounts_export.csv", header, csv_rows)

    async def bot_subscribers(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("Invalid bot_id", 400)
        owns = await _safe_count(pool,
            """SELECT COUNT(*) FROM managed_bots mb
               WHERE mb.bot_id=$1 AND (
                   mb.added_by=$2
                   OR mb.bot_id IN (
                       SELECT DISTINCT eb.bot_id FROM ecosystem_bots eb
                       JOIN ecosystems e ON e.id=eb.ecosystem_id
                       WHERE e.owner_id=$2
                          OR e.id IN (SELECT ecosystem_id FROM ecosystem_members WHERE owner_id=$2)
                   )
                   OR mb.bot_id IN (
                       SELECT DISTINCT b.bot_id FROM managed_bots b
                       JOIN workspaces w ON w.owner_id=b.added_by
                       JOIN workspace_members wm ON wm.workspace_id=w.id
                       WHERE wm.user_id=$2
                   )
               )""", bot_id, uid)
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
        name = validate_string(body.get("name"), max_len=200)
        text_template = validate_string(body.get("text_template"), max_len=4096)
        target_type = validate_string(body.get("target_type"), max_len=30) or "all_bots"
        target_id = body.get("target_id")
        if not name:
            return _err("name required")
        if not text_template:
            return _err("text_template required")
        if check_sql_suspicious(text_template):
            return _err("Invalid characters in message text")
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
        text = validate_string(body.get("text"), max_len=4096)
        if not text:
            return _err("text required")
        if check_sql_suspicious(text):
            return _err("Invalid characters in post text")
        if len(text) > 4096:
            return _err("Message too long (max 4096 chars)")
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Channel not found", 404)
        if not ch.get("acc_id"):
            return _err("No linked account for this channel", 400)
        # Отложенная публикация: schedule_minutes минут от текущего момента.
        try:
            schedule_minutes = max(0, min(int(body.get("schedule_minutes") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            schedule_minutes = 0
        sched = None
        if schedule_minutes > 0:
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            sched = (_dt.now(_tz.utc) + _td(minutes=schedule_minutes)).isoformat()
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
            }, total_items=1, scheduled_for=sched)
            return _json_resp({"ok": True, "op_id": op_id, "scheduled_minutes": schedule_minutes})
        except Exception:
            log.exception("post_to_channel ch=%d uid=%d", ch_id, uid)
            return _err("Failed to enqueue post", 500)

    async def pin_channel_last_post(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Invalid ch_id", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels "
            "WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
        if not ch:
            return _err("Channel not found", 404)
        if not ch.get("acc_id"):
            return _err("No linked account for this channel", 400)
        try:
            from services.operation_bus import submit
            op_id = await submit(pool, uid, "pin_last_post", {
                "channel_ref": int(ch_id),
                "account_id": int(ch["acc_id"]),
                "access_hash": int(ch.get("access_hash") or 0),
            }, total_items=1)
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception:
            log.exception("pin_channel_last_post ch=%d uid=%d", ch_id, uid)
            return _err("Failed to enqueue pin", 500)

    async def channel_invite_link(request: web.Request) -> web.Response:
        """Сгенерировать/получить инвайт-ссылку канала (инлайн, один Telethon-вызов).
        Возможность account_manager.get_channel_invite_link раньше была недоступна в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            ch_id = int(request.match_info["ch_id"])
        except (KeyError, ValueError):
            return _err("Invalid ch_id", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id, access_hash FROM managed_channels "
            "WHERE channel_id=$1 AND owner_id=$2", ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", int(ch["acc_id"]), uid)
        if not acc or not acc.get("session_str"):
            return _err("Привязанный аккаунт недоступен", 400)
        try:
            from services import account_manager
            link = await account_manager.get_channel_invite_link(
                acc["session_str"], int(ch_id), _acc=dict(acc),
                access_hash=int(ch.get("access_hash") or 0),
            )
            if not link:
                return _err("Не удалось получить ссылку (нужны права администратора у аккаунта)", 400)
            return _json_resp({"ok": True, "invite_link": link, "title": ch.get("title") or ""})
        except Exception:
            log.exception("channel_invite_link ch=%d uid=%d", ch_id, uid)
            return _err("Ошибка получения ссылки", 500)

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
        # ── Статистика роста участников (снимок за день + дельты) ─────────────
        mc = int(ch.get("member_count") or 0)
        if mc > 0:
            try:
                await pool.execute(
                    """INSERT INTO channel_member_history(owner_id, channel_id, members_count)
                       VALUES($1,$2,$3)
                       ON CONFLICT (owner_id, channel_id, captured_on)
                       DO UPDATE SET members_count=EXCLUDED.members_count, captured_at=NOW()""",
                    uid, ch_id, mc)
            except Exception:
                log.debug("channel_detail: member snapshot failed ch=%d", ch_id)
        hist = await _safe_fetch(pool,
            """SELECT members_count, captured_on FROM channel_member_history
               WHERE owner_id=$1 AND channel_id=$2
               ORDER BY captured_on DESC LIMIT 30""", uid, ch_id)
        def _delta(days):
            from datetime import date as _date, timedelta as _tdd
            cutoff = _date.today() - _tdd(days=days)
            for r in hist:
                if r["captured_on"] <= cutoff:
                    return mc - int(r["members_count"])
            return None
        member_growth = {
            "current": mc,
            "points": len(hist),
            "d1": _delta(1),
            "d7": _delta(7),
            "d30": _delta(30),
            "series": [
                {"d": r["captured_on"].isoformat(), "c": int(r["members_count"])}
                for r in reversed(hist)
            ],
        }
        return _json_resp({
            "channel": ch,
            "linked_account": acc,
            "recent_ops": recent_ops,
            "member_growth": member_growth,
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

    async def operation_status(request: web.Request) -> web.Response:
        """Статус одной операции (для инлайн-опроса результата после enqueue)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("bad op_id", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, op_type, status, label, total_items, done_items, error_msg, "
            "(result->>'summary') AS summary "
            "FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, uid)
        if not row:
            return _err("Операция не найдена", 404)
        return _json_resp(dict(row))

    async def operation_log(request: web.Request) -> web.Response:
        """Пошаговый лог одной операции (per-target: канал/бот/аккаунт → статус).

        Фронт (openOpDetail) рендерит секцию «📋 Лог» из этого ответа. Раньше
        маршрут не был зарегистрирован → api() ловил 404 через .catch и секция
        лога ВСЕГДА была пустой (мёртвая кнопка). Скоуп по owner_id обязателен —
        иначе можно прочитать лог чужой операции по её id.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            op_id = int(request.match_info["op_id"])
        except (KeyError, ValueError):
            return _err("bad op_id", 400)
        # Проверяем владение операцией перед чтением её лога.
        owns = await _safe_fetchrow(pool,
            "SELECT 1 FROM operation_queue WHERE id=$1 AND owner_id=$2", op_id, uid)
        if not owns:
            return _err("Операция не найдена", 404)
        rows = await _safe_fetch(pool,
            "SELECT step_num, target, status, message, created_at "
            "FROM operation_log WHERE op_id=$1 "
            "ORDER BY step_num, id LIMIT 500", op_id)
        return _json_resp({"logs": rows})

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
        except Exception as e:
            log.warning("cancel_operation op=%d uid=%d: %s", op_id, uid, e)
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
        name = validate_string(body.get("name"), max_len=200)
        start_param = validate_string(body.get("start_param"), max_len=64)
        if not name or not start_param:
            return _err("name and start_param required")
        if not validate_start_param(start_param):
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
        except Exception as e:
            log.warning("delete_deeplink link=%d uid=%d: %s", link_id, uid, e)
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
        except Exception as e:
            log.warning("bot_engagement user_activity uid=%d bot=%d: %s", uid, bot_id, e)
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
            except Exception as e2:
                log.warning("bot_engagement bot_users uid=%d bot=%d: %s", uid, bot_id, e2)
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
            note = validate_string(body.get("note"), max_len=2000, required=False)
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

    async def bot_profile(request: web.Request) -> web.Response:
        """Изменить профиль бота: имя, описание, краткое описание (Bot API)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        name = (body.get("name") or "").strip()[:64]
        description = (body.get("description") or "").strip()[:512]
        short_description = (body.get("short_description") or "").strip()[:120]
        if not (name or description or short_description):
            return _err("Укажите хотя бы одно поле", 400)
        row = await _safe_fetchrow(pool,
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not row:
            return _err("Бот не найден", 404)
        try:
            from services.token_vault import decrypt_token as _dt
            token = _dt(row["token"])
        except Exception:
            token = row["token"]
        import aiohttp as _aio
        results: dict = {}
        calls = []
        if name:
            calls.append(("setMyName", {"name": name}, "name"))
        if description:
            calls.append(("setMyDescription", {"description": description}, "description"))
        if short_description:
            calls.append(("setMyShortDescription", {"short_description": short_description}, "short_description"))
        try:
            async with _aio.ClientSession() as sess:
                for method, params, key in calls:
                    try:
                        async with sess.post(
                            f"https://api.telegram.org/bot{token}/{method}",
                            json=params, timeout=_aio.ClientTimeout(total=10),
                        ) as resp:
                            jd = await resp.json()
                        results[key] = bool(jd.get("ok"))
                        if not jd.get("ok"):
                            results[key + "_error"] = jd.get("description", "error")
                    except Exception as e:
                        results[key] = False
                        results[key + "_error"] = str(e)[:80]
        except Exception as exc:
            log.exception("bot_profile uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)
        ok_any = any(v is True for k, v in results.items() if not k.endswith("_error"))
        return _json_resp({"ok": ok_any, "results": results})

    async def bot_avatar(request: web.Request) -> web.Response:
        """Сменить/удалить аватар бота (bot_api.set_photo/delete_my_photo — были мертвы).
        POST {photo_url}: скачать по URL (SSRF-гард) и загрузить. DELETE: удалить фото."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        row = await _safe_fetchrow(pool,
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
        if not row:
            return _err("Бот не найден", 404)
        try:
            from services.token_vault import decrypt_token as _dt
            token = _dt(row["token"])
        except Exception:
            token = row["token"]
        from services import bot_api
        import aiohttp as _aio

        if request.method == "DELETE":
            try:
                async with _aio.ClientSession() as sess:
                    ok = await bot_api.delete_my_photo(sess, token)
                return _json_resp({"ok": bool(ok)})
            except Exception:
                log.exception("bot_avatar delete uid=%d bot=%d", uid, bot_id)
                return _err("Ошибка удаления аватара", 500)

        try:
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        photo_url = (body.get("photo_url") or "").strip()
        if not is_safe_public_url(photo_url):
            return _err("Нужен публичный https URL картинки", 400)
        try:
            async with _aio.ClientSession() as sess:
                # Скачиваем с потолком размера (5 МБ) и таймаутом.
                async with sess.get(photo_url, timeout=_aio.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return _err(f"Не удалось скачать картинку (HTTP {resp.status})", 400)
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "image" not in ctype:
                        return _err("URL не является картинкой", 400)
                    data = await resp.content.read(5 * 1024 * 1024 + 1)
                    if len(data) > 5 * 1024 * 1024:
                        return _err("Картинка слишком большая (макс. 5 МБ)", 400)
                ok = await bot_api.set_photo(sess, token, data)
            if ok:
                return _json_resp({"ok": True})
            return _err("Telegram отклонил картинку (формат/размер)", 400)
        except Exception:
            log.exception("bot_avatar set uid=%d bot=%d", uid, bot_id)
            return _err("Ошибка установки аватара", 500)

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
            op = validate_string(body.get("op"), max_len=20)
            acc_count = validate_integer(body.get("acc_count", 0), min_val=0) or 0
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
            op = validate_string(body.get("op"), max_len=30)
            account_id = validate_integer(body.get("account_id"), min_val=1)
        except Exception:
            return _err("bad body", 400)
        if not op or not account_id:
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
            except Exception as e:
                log.warning("topology_overview links uid=%d: %s", uid, e)
                links = 0
            # Bot-user relationships
            try:
                bot_users_total = await pool.fetchval(
                    """SELECT COUNT(*) FROM bot_users bu
                       JOIN managed_bots b ON b.bot_id=bu.bot_id
                       WHERE b.added_by=$1""",
                    uid,
                )
            except Exception as e:
                log.warning("topology_overview bot_users uid=%d: %s", uid, e)
                bot_users_total = 0
            return _json_resp({
                "accounts": accs, "channels": channels, "bots": bots,
                "channel_links": links, "bot_users_total": bot_users_total,
            })
        except Exception as exc:
            log.exception("topology_overview uid=%d", uid)
            return _err(str(exc), 500)

    async def topology_links(request: web.Request) -> web.Response:
        """Drill-down for the Topology Map: which account owns which channels/groups,
        and per-bot subscriber counts — topology_overview only returns flat totals."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT a.id AS acc_id, a.phone, a.first_name,
                          mc.id AS channel_pk, mc.channel_id, mc.title, mc.username
                   FROM tg_accounts a
                   LEFT JOIN managed_channels mc
                       ON mc.acc_id = a.id AND mc.owner_id = a.owner_id
                   WHERE a.owner_id=$1
                   ORDER BY a.id, mc.title""",
                uid,
            )
            by_acc: dict[int, dict] = {}
            for r in rows:
                acc = by_acc.setdefault(r["acc_id"], {
                    "acc_id": r["acc_id"],
                    "label": f"@{r['phone']}" if not r["first_name"] else r["first_name"],
                    "channels": [],
                })
                if r["channel_pk"] is not None:
                    acc["channels"].append({
                        "id": r["channel_pk"],
                        "channel_id": r["channel_id"],
                        "title": r["title"] or "",
                        "username": r["username"] or "",
                    })
            bot_rows = await pool.fetch(
                """SELECT b.bot_id, b.username, b.first_name,
                          COUNT(bu.user_id) FILTER (WHERE bu.is_active) AS subs
                   FROM managed_bots b
                   LEFT JOIN bot_users bu ON bu.bot_id = b.bot_id
                   WHERE b.added_by=$1
                   GROUP BY b.bot_id, b.username, b.first_name
                   ORDER BY subs DESC NULLS LAST""",
                uid,
            )
            return _json_resp({
                "accounts": list(by_acc.values()),
                "bots": [
                    {
                        "bot_id": r["bot_id"],
                        "label": f"@{r['username']}" if r["username"] else (r["first_name"] or ""),
                        "subscribers": r["subs"] or 0,
                    }
                    for r in bot_rows
                ],
            })
        except Exception as exc:
            log.exception("topology_links uid=%d", uid)
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
        except Exception as e:
            log.warning("infra_analytics active_accs uid=%d: %s", uid, e)
            active_accs = 0
        try:
            flood_24h = await pool.fetchval(
                """SELECT COUNT(*) FROM account_flood_log fl
                   JOIN tg_accounts ta ON ta.id=fl.account_id
                   WHERE ta.owner_id=$1 AND fl.created_at > NOW()-INTERVAL '24h'""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics flood_24h uid=%d: %s", uid, e)
            flood_24h = 0
        try:
            ops_24h = await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND created_at > NOW()-INTERVAL '24h'",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics ops_24h uid=%d: %s", uid, e)
            ops_24h = 0
        try:
            warmup_active = await pool.fetchval(
                """SELECT COUNT(*) FROM account_warmup_plans wp
                   WHERE wp.owner_id=$1 AND wp.status='active'""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics warmup uid=%d: %s", uid, e)
            warmup_active = 0
        try:
            pools = await pool.fetch(
                """SELECT pool, COUNT(*) AS cnt FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=TRUE AND pool IS NOT NULL
                   GROUP BY pool ORDER BY cnt DESC LIMIT 10""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics pools uid=%d: %s", uid, e)
            pools = []
        try:
            audit_rows = await pool.fetch(
                """SELECT action, target, result, occurred_at
                   FROM operation_audit WHERE owner_id=$1
                   ORDER BY occurred_at DESC LIMIT 10""",
                uid,
            )
        except Exception as e:
            log.warning("infra_analytics audit uid=%d: %s", uid, e)
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
        """Массовая проверка аккаунтов (админ: все, обычный: свои)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        admin = _is_admin(uid)
        if admin:
            rows = await _safe_fetch(pool, "SELECT id FROM tg_accounts WHERE is_active=TRUE")
        else:
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
        if op == "username":
            un = (body.get("username") or "").strip().lstrip("@")
            if not un:
                return None, "Укажите username"
            params["username"] = un
            return params, "Смена username"
        if op == "close_sessions":
            return params, "Закрыть сторонние сессии"
        if op == "privacy":
            key = (body.get("privacy_key") or "").strip()
            if key not in ("phone", "invite", "lastseen"):
                return None, "Неизвестный ключ приватности"
            params["privacy_key"] = key
            params["privacy_allow"] = bool(body.get("privacy_allow"))
            return params, "Настройка приватности"
        if op == "reset_2fa":
            cp = (body.get("current_password") or "").strip()
            if not cp:
                return None, "Укажите текущий пароль 2FA для снятия"
            params["current_password"] = cp
            return params, "Снятие 2FA"
        if op in ("clear_bio", "remove_username", "remove_avatar", "set_online", "check_restriction"):
            return params, {"clear_bio": "Очистка bio", "remove_username": "Снятие username",
                            "remove_avatar": "Удаление фото", "set_online": "В сети",
                            "check_restriction": "Проверка ограничений"}[op]
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
        admin = _is_admin(uid)
        # Режим «применить ко всему срезу»: id берём не из загруженной страницы,
        # а резолвим весь набор под текущим фильтром на сервере (тот же WHERE, что
        # и список) со скоупом owner/admin. Снимает 100-лимит для масс-операций.
        if body.get("select_all_filtered"):
            flt = body.get("filter", "all")
            if flt not in ("all", "active", "cooldown", "banned"):
                flt = "all"
            stage = (body.get("stage") or "").strip().lower()
            if stage and stage not in ACCOUNT_STAGES:
                stage = ""
            qterm = (body.get("q") or "").strip()[:64]
            where, wargs = _accounts_where(uid, flt, stage, qterm, admin=admin)
            owned = await _safe_fetch(pool,
                f"SELECT id FROM tg_accounts WHERE {where} ORDER BY id LIMIT 5000", *wargs)
            ids = [int(r["id"]) for r in (owned or [])]
            if not ids:
                return _err("Под фильтром нет аккаунтов", 404)
        else:
            ids_in = [int(x) for x in (body.get("account_ids") or []) if str(x).lstrip("-").isdigit()]
            if not ids_in:
                return _err("Выберите аккаунты", 400)
            if admin:
                owned = await _safe_fetch(pool,
                    "SELECT id FROM tg_accounts WHERE id = ANY($1::bigint[])",
                    ids_in)
            else:
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
            if op == "set_stage":
                # Пере­мещение в CRM-статус — чистое DB-действие, без очереди/Telethon.
                raw = body.get("stage")
                stage = (str(raw).strip().lower() or None) if raw is not None else None
                if stage is not None and stage not in ACCOUNT_STAGES:
                    return _err("Неизвестный статус", 400)
                await pool.execute(
                    "UPDATE tg_accounts SET stage=$1 WHERE owner_id=$2 AND id=ANY($3::bigint[])",
                    stage, uid, ids)
                return _json_resp({"ok": True, "count": n, "stage": stage})
            if op == "detach_proxy":
                # Снять прокси у выбранных аккаунтов — чтобы работать БЕЗ прокси
                # (прямое соединение). Чистое DB-действие. После этого аккаунт
                # не привязан к IP прокси и подключается напрямую.
                await pool.execute(
                    "UPDATE tg_accounts SET proxy_id=NULL WHERE owner_id=$1 AND id=ANY($2::bigint[])",
                    uid, ids)
                return _json_resp({"ok": True, "count": n})
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
            if op in ("name", "avatar", "2fa", "username", "close_sessions", "privacy",
                      "clear_bio", "remove_username", "remove_avatar", "reset_2fa",
                      "set_online", "check_restriction"):
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
        elif op == "username":
            un = (body.get("username") or "").strip().lstrip("@")
            if not un:
                return _err("Укажите username", 400)
            params["username"] = un
            label = "Смена username"
        elif op == "close_sessions":
            label = "Закрыть сторонние сессии"
        elif op == "privacy":
            pk = (body.get("privacy_key") or "phone").strip()
            if pk not in ("phone", "invite", "lastseen"):
                return _err("privacy_key: phone|invite|lastseen", 400)
            params["privacy_key"] = pk
            params["privacy_allow"] = bool(body.get("privacy_allow", False))
            label = f"Приватность: {pk}"
        elif op == "reset_2fa":
            cp = (body.get("current_password") or "").strip()
            if not cp:
                return _err("Укажите текущий пароль 2FA для снятия", 400)
            params["current_password"] = cp
            label = "Снятие 2FA"
        elif op in ("clear_bio", "remove_username", "remove_avatar", "set_online", "check_restriction"):
            label = {"clear_bio": "Очистка bio", "remove_username": "Снятие username",
                     "remove_avatar": "Удаление фото", "set_online": "В сети",
                     "check_restriction": "Проверка ограничений"}[op]
        else:
            return _err("Неизвестная операция", 400)
        # Единичный аккаунт — исполняем ИНЛАЙН с немедленным результатом (как
        # check_restriction). Раньше op просто ставился в очередь и молча падал в
        # фоне при мёртвой сессии/прокси → пользователь видел «ничего не происходит».
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            res = await asyncio.wait_for(
                pse.apply_op(acc["session_str"], dict(acc), op, params), timeout=45)
            if res.get("ok"):
                return _json_resp({"ok": True, "result": res, "label": label})
            return _err(res.get("error") or "Операция не выполнена", 400)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 45с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_profile inline uid=%d acc=%d op=%s", uid, acc_id, op)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)

    async def global_search(request: web.Request) -> web.Response:
        """Global Search — глобальный поиск публичных каналов/групп/ботов/юзеров
        по строке-запросу (Telethon contacts.SearchRequest). Раздел 12 паритета TE.
        Исполняется инлайн реальным аккаунтом владельца, результат — сразу."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        query = sanitize_search_query(body.get("query") or "")
        if not query:
            return _err("Укажите поисковый запрос", 400)
        try:
            limit = max(1, min(validate_integer(body.get("limit") or 20, min_val=1, max_val=50) or 20, 50))
        except (TypeError, ValueError):
            limit = 20
        # Аккаунт: указанный (если принадлежит владельцу и активен) либо первый активный.
        acc_id = body.get("account_id")
        base = (
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        )
        if acc_id:
            acc = await _safe_fetchrow(pool, base + "AND id=$2 LIMIT 1", uid, int(acc_id))
        else:
            acc = await _safe_fetchrow(pool, base + "ORDER BY last_used DESC NULLS LAST LIMIT 1", uid)
        if not acc or not acc.get("session_str"):
            return _err("Нет активного аккаунта для поиска — добавьте аккаунт", 400)
        try:
            from services import global_search_engine as gse
            res = await asyncio.wait_for(
                gse.search_public(acc["session_str"], query, limit, _acc=dict(acc)),
                timeout=40,
            )
            if res.get("ok"):
                return _json_resp({"ok": True, "results": res.get("results", []), "query": query})
            return _err(res.get("error") or "Поиск не выполнен", 400)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 40с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("global_search uid=%d q=%r", uid, query)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)

    async def account_login_code(request: web.Request) -> web.Response:
        """Получить последний код входа Telegram для аккаунта (инлайн, из чата 777000).
        Аналог «Получить код авторизации» — раньше в mini-app недоступно."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            # Таймаут на инлайн-коннект — иначе зависание → edge 502/520.
            res = await asyncio.wait_for(
                pse.get_login_code(acc["session_str"], {**dict(acc), "_low_risk": True}), timeout=30
            )
            if res.get("ok"):
                return _json_resp({"ok": True, "code": res["code"]})
            return _err(res.get("error") or "Код не найден", 404)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 30с — проверьте прокси/сессию", 400)
        except Exception:
            log.exception("account_login_code uid=%d acc=%d", uid, acc_id)
            return _err("Ошибка получения кода", 400)

    async def account_check_restriction(request: web.Request) -> web.Response:
        """Инлайн-проверка ограничений одного аккаунта (жив/ограничен/удалён).
        Аналог раздела «ПРОВЕРКА» Telegram Expert — результат сразу, без очереди."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import profile_setter_engine as pse
            # Инлайн-коннект к Telegram с ЖЁСТКИМ таймаутом: без него зависший
            # коннект (мёртвая сессия/плохой прокси) висит до edge-таймаута и
            # отдаёт 502/520 → фронт показывает «Сервис временно недоступен».
            res = await asyncio.wait_for(
                pse.check_restriction(acc["session_str"], {**dict(acc), "_low_risk": True}), timeout=30
            )
            if not res.get("ok"):
                # Бизнес-ошибка (не смогли проверить), НЕ 502 — иначе фронт покажет
                # «сервис недоступен» вместо реальной причины.
                return _err(res.get("error") or "Не удалось проверить аккаунт", 400)
            return _json_resp({
                "ok": True,
                "alive": res.get("alive"),
                "restricted": res.get("restricted"),
                "deleted": res.get("deleted"),
                "reason": res.get("reason"),
                "username": res.get("username"),
                "user_id": res.get("user_id"),
            })
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 30с — проверьте прокси/сессию", 400)
        except Exception:
            log.exception("account_check_restriction uid=%d acc=%d", uid, acc_id)
            return _err("Ошибка проверки аккаунта", 400)

    async def accounts_export_json(request: web.Request) -> web.Response:
        """Экспорт МЕТАДАННЫХ аккаунтов пользователя в JSON (аналог «РАБОТА С JSON»).
        Секреты (session_str, proxy-креды) НАМЕРЕННО не выгружаются — иначе это
        обнулило бы шифрование at-rest (см. AUDIT_LEDGER, разрыв №1). Экспортируем
        то, что реально нужно для учёта: id, телефон, имя, username, кластер, статус,
        есть ли прокси. Импорт tdata/session — отдельный защищённый путь."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        base = ("SELECT id, phone, first_name, username, cluster, acc_status, "
                "real_check_status, warmup_level, is_active, "
                "(proxy_id IS NOT NULL) AS has_proxy, added_at "
                "FROM tg_accounts WHERE owner_id=$1 ORDER BY id")
        try:
            rows = await pool.fetch(base, uid)
        except Exception:
            # оборонительный фолбэк на гарантированно существующие колонки
            rows = await _safe_fetch(pool,
                "SELECT id, phone, first_name, username, is_active, "
                "(proxy_id IS NOT NULL) AS has_proxy FROM tg_accounts "
                "WHERE owner_id=$1 ORDER BY id", uid)
        out = []
        for r in (rows or []):
            d = dict(r)
            ca = d.get("added_at")
            if hasattr(ca, "isoformat"):
                d["added_at"] = ca.isoformat()
            out.append(d)
        return _json_resp({"ok": True, "count": len(out), "accounts": out})

    async def channel_edit(request: web.Request) -> web.Response:
        """Изменить название/описание/username канала (op: title|about|username).
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
        worker_op = channel_edit_worker_op(op)
        if not worker_op or not value:
            return _err("Укажите op (title|about|username) и значение", 400)
        if op == "title" and len(value) > 128:
            return _err("Название канала — до 128 символов", 400)
        ch = await _safe_fetchrow(pool,
            "SELECT channel_id, title, acc_id FROM managed_channels WHERE channel_id=$1 AND owner_id=$2",
            ch_id, uid)
        if not ch:
            return _err("Канал не найден", 404)
        if not ch.get("acc_id"):
            return _err("У канала нет привязанного аккаунта", 400)
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
            if channel_edit_worker_op(op):
                value = (body.get("value") or "").strip()
                if not value:
                    return _err("Укажите значение", 400)
                if op == "title" and len(value) > 128:
                    return _err("Название канала — до 128 символов", 400)
                worker_op = channel_edit_worker_op(op)
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

    async def channel_add(request: web.Request) -> web.Response:
        """Вступить в существующий канал/группу по ссылке и добавить в управление."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad body", 400)
        link = validate_string(body.get("channel_identifier"), max_len=500)
        if not link:
            return _err("channel_identifier required")
        # Validate URL format for channel identifier
        if not link.startswith(("http", "@", "t.me")):
            return _err("Invalid channel identifier format")
        try:
            from bot.utils.subscription import get_channel_limit, get_effective_channel_count
            _lim = await get_channel_limit(pool, uid)
            if await get_effective_channel_count(pool, uid) >= _lim:
                return _err(f"Достигнут лимит каналов ({_lim}) для вашего тарифа. Оформите подписку для снятия ограничений.", 403)
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
                "VALUES($1,'channel_add','pending',$2,1,$3) RETURNING id",
                uid, json.dumps({"channel_identifier": link}),
                f"Добавить канал: {link}",
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("channel_add uid=%d", uid)
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
        elif act == "read_all":
            op_type, params, label = "read_all_dialogs", {"account_id": acc_id}, "Прочитать все диалоги"
        elif act == "delete_pm":
            op_type, params, label = "delete_private_dialogs", {"account_id": acc_id}, "Удаление личных диалогов"
        elif act == "reset_cooldown":
            # СИНХРОННО (раньше был фантомный op reset_cooldown без исполнителя →
            # кнопка ничего не делала). Чистим durable-кулдаун.
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET cooldown_until=NULL WHERE id=$1 AND owner_id=$2",
                    acc_id, uid)
                return _json_resp({"ok": True, "message": "⚡ Кулдаун сброшен"})
            except Exception as exc:
                log.exception("reset_cooldown uid=%d acc=%d", uid, acc_id)
                return _err(str(exc)[:120], 500)
        elif act == "export_session":
            # СИНХРОННО (раньше фантомный op → r.session никогда не приходил).
            # Владелец экспортирует СВОЮ сессию (owner-scoped), расшифровываем.
            row = await _safe_fetchrow(pool,
                "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
            if not row or not row["session_str"]:
                return _err("Сессия недоступна", 404)
            try:
                from services.token_vault import decrypt_token
                sess = decrypt_token(row["session_str"])
            except Exception:
                sess = row["session_str"]
            return _json_resp({"ok": True, "session": sess})
        elif act == "reauth":
            # Переавторизация требует ОДНОРАЗОВЫЙ КОД из Telegram (вводится вручную) —
            # фоновой операцией невозможно. Раньше ставился фантомный op reauth_account
            # (нет исполнителя) → «запущена», но ничего не происходило. Честно ведём в
            # бота, где flow релога реально работает (шлёт код → приём кода → вход).
            return _json_resp({"ok": True, "message": (
                "🔑 Переавторизация требует код из Telegram (вводится вручную), поэтому "
                "выполняется в боте: Аккаунты → 🔄 Переавторизатор → выберите этот аккаунт "
                "→ Релог. После входа аккаунт снова станет активным.")})
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

    async def account_post_story(request: web.Request) -> web.Response:
        """Story Manager: опубликовать историю на СВОЙ аккаунт из media_url.
        body: {media_url, caption?, period_hours?}. Инлайн, немедленный итог."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        media_url = (body.get("media_url") or "").strip()
        if not media_url:
            return _err("Укажите ссылку на медиа (фото/видео)", 400)
        caption = (body.get("caption") or "").strip()
        period_hours = body.get("period_hours", 24)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import story_manager
            res = await asyncio.wait_for(
                story_manager.post_story(acc["session_str"], media_url, caption, period_hours, dict(acc)),
                timeout=180)
        except asyncio.TimeoutError:
            return _err("Публикация не завершилась за 180с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_post_story uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        if res.get("ok"):
            return _json_resp({"ok": True, "status": res.get("status"),
                               "label": f"✅ История опубликована ({res.get('period_hours', 24)}ч)"})
        return _err(res.get("error") or "Не удалось опубликовать историю", 400)

    async def account_spamblock_appeal(request: web.Request) -> web.Response:
        """Снятие спамблока: запрос в @SpamBot с проходом по кнопкам аппеляции.
        Инлайн, немедленный результат (реабилитация своего аккаунта)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
        except (KeyError, ValueError):
            return _err("bad acc_id", 400)
        acc = await _safe_fetchrow(pool,
            "SELECT id, session_str, device_model, system_version, app_version, "
            "lang_code, system_lang_code, "
            "(SELECT proxy_url FROM user_proxies up WHERE up.id=tg_accounts.proxy_id AND up.is_active=TRUE) AS proxy_url "
            "FROM tg_accounts WHERE id=$1 AND owner_id=$2 AND is_active=TRUE", acc_id, uid)
        if not acc or not acc.get("session_str"):
            return _err("Аккаунт недоступен", 400)
        try:
            from services import account_manager
            res = await asyncio.wait_for(
                account_manager.appeal_spamblock(acc["session_str"], dict(acc)), timeout=60)
        except asyncio.TimeoutError:
            return _err("Аккаунт не ответил за 60с — проверьте прокси/сессию", 400)
        except Exception as exc:
            log.exception("account_spamblock_appeal uid=%d acc=%d", uid, acc_id)
            return _err(f"Ошибка: {str(exc)[:140]}", 400)
        # Синхронизируем acc_status в БД, если бот подтвердил «free».
        if res.get("status") == "free":
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET acc_status='ok' WHERE id=$1 AND owner_id=$2", acc_id, uid)
            except Exception:
                pass
        labels = {"free": "✅ Спамблок снят — аккаунт активен",
                  "appeal_sent": "📨 Аппеляция отправлена — ждите решения",
                  "still_blocked": "🔴 Спамблок активен, кнопки аппеляции нет",
                  "error": "⚠️ Ошибка обращения к @SpamBot",
                  "no_session": "⚠️ Сессия недоступна"}
        if not res.get("ok") and res.get("status") not in ("still_blocked", "appeal_sent", "free"):
            return _err(labels.get(res.get("status"), "Не выполнено") + (f": {res.get('reply')}" if res.get("reply") else ""), 400)
        return _json_resp({"ok": True, "status": res.get("status"),
                           "label": labels.get(res.get("status"), res.get("status")),
                           "reply": res.get("reply"), "steps": res.get("steps", 0)})

    async def account_set_proxy(request: web.Request) -> web.Response:
        """Назначить/снять прокси у аккаунта. body: {proxy_id: int|null}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        raw = body.get("proxy_id")
        proxy_id = None
        if raw not in (None, "", 0, "0"):
            try:
                proxy_id = int(raw)
            except (TypeError, ValueError):
                return _err("Invalid proxy_id", 400)
            owns_proxy = await _safe_count(pool,
                "SELECT COUNT(*) FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
            if not owns_proxy:
                return _err("Прокси не найден", 404)
        try:
            await pool.execute(
                "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3",
                proxy_id, acc_id, uid)
            return _json_resp({"ok": True, "proxy_id": proxy_id})
        except Exception as exc:
            log.exception("account_set_proxy uid=%d acc=%d", uid, acc_id)
            return _err(str(exc), 500)

    async def account_set_note(request: web.Request) -> web.Response:
        """Заметка к аккаунту (account_notes)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        note = validate_string(body.get("note"), max_len=500, required=False)
        try:
            res = await pool.execute(
                "UPDATE tg_accounts SET account_notes=$1 WHERE id=$2 AND owner_id=$3",
                note or None, acc_id, uid)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("account_set_note uid=%d acc=%d", uid, acc_id)
            return _err(str(exc), 500)

    async def account_set_meta(request: web.Request) -> web.Response:
        """Привязка аккаунта к кластеру и/или переименование метки (first_name)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            acc_id = int(request.match_info["acc_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE id=$1 AND owner_id=$2", acc_id, uid)
        if not owns:
            return _err("Аккаунт не найден", 404)
        sets: list = []
        args: list = []
        idx = 1
        if "cluster" in body:
            raw = body.get("cluster")
            cluster = validate_string(raw, max_len=64, required=False)
            sets.append(f"cluster=${idx}"); args.append(cluster); idx += 1
        if "label" in body:
            label = validate_string(body.get("label"), max_len=128)
            if not label:
                return _err("Метка не может быть пустой", 400)
            sets.append(f"first_name=${idx}"); args.append(label); idx += 1
        if "stage" in body:
            raw = body.get("stage")
            stage = validate_string(raw, max_len=20, required=False)
            if stage:
                stage = stage.lower()
            if stage is not None and stage not in ACCOUNT_STAGES:
                return _err("Неизвестный статус", 400)
            sets.append(f"stage=${idx}"); args.append(stage); idx += 1
        if not sets:
            return _err("Нечего обновлять", 400)
        args.extend([acc_id, uid])
        sql = (f"UPDATE tg_accounts SET {', '.join(sets)} "
               f"WHERE id=${idx} AND owner_id=${idx+1}")
        try:
            res = await pool.execute(sql, *args)
            if str(res).endswith(" 0"):
                return _err("Аккаунт не найден", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("account_set_meta uid=%d acc=%d", uid, acc_id)
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
        """Накрутка: просмотры / реакции / сторис / подписчики / старты в ботах."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        btype = validate_string(body.get("type"), max_len=20)
        if btype not in ("views", "reactions", "stories", "subscribers", "bot_starts"):
            return _err("Неверный тип накрутки", 400)
        try:
            acc_count = validate_integer(body.get("acc_count") or 0, min_val=0) or 0
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
        elif btype == "stories":
            target = (body.get("target") or "").strip()
            if not target:
                return _err("Укажите цель (@username)", 400)
            op_type = "boost_stories"
            params = {"target": target, "account_ids": account_ids}
            total = len(account_ids)
            label = f"Сторис: {target} × {len(account_ids)} акк."
        elif btype == "subscribers":
            from services.boost_engine import parse_channel_ref
            target = parse_channel_ref((body.get("target") or "").strip())
            premium_only = bool(body.get("premium_only"))
            if not target:
                return _err("Укажите канал/группу (@username или t.me/link)", 400)
            op_type = "boost_subscribers"
            params = {"target": target, "account_ids": account_ids, "premium_only": premium_only}
            total = len(account_ids)
            prem_suffix = " (только Premium)" if premium_only else ""
            label = f"Подписчики/участники: {target} × {len(account_ids)} акк.{prem_suffix}"
        else:  # bot_starts
            from services.boost_engine import parse_channel_ref
            bot_username = parse_channel_ref((body.get("bot_username") or "").strip()).lstrip("@")
            payload = (body.get("payload") or "").strip() or None
            premium_only = bool(body.get("premium_only"))
            if not bot_username:
                return _err("Укажите @username бота", 400)
            op_type = "boost_bot_starts"
            params = {
                "bot_username": bot_username,
                "payload": payload,
                "account_ids": account_ids,
                "premium_only": premium_only,
            }
            total = len(account_ids)
            prem_suffix = " (только Premium)" if premium_only else ""
            label = f"Старты в боте: @{bot_username} × {len(account_ids)} акк.{prem_suffix}"

        try:
            if op_type in ("boost_subscribers", "boost_bot_starts"):
                from services import operation_bus
                op_id = await operation_bus.submit(pool, uid, op_type, params, total_items=total)
            else:
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
        """Growth Agent: постинг промо-текста в нишевых группах."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        niche = validate_string(body.get("niche"), max_len=200)
        geo = validate_string(body.get("geo"), max_len=60) or ""
        promo_text = validate_string(body.get("promo_text"), max_len=4096)
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 3, min_val=1, max_val=10) or 3, 10))
        except (TypeError, ValueError):
            acc_count = 3
        if not niche:
            return _err("Укажите нишу", 400)
        if not promo_text:
            return _err("Укажите рекламный текст", 400)
        if check_sql_suspicious(promo_text):
            return _err("Invalid characters in promo text")
        from services import content_safety

        _v = await content_safety.enforce(pool, uid, niche, promo_text, surface="growth_agent")
        if _v.blocked:
            return _err("Контент заблокирован политикой платформы", 403)
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
                uid,
                _json.dumps({"niche": niche, "geo": geo, "promo_text": promo_text, "acc_count": acc_count}),
                label,
            )
            return _json_resp({"ok": True, "op_id": op_id, "label": label})
        except Exception as exc:
            log.exception("growth_submit uid=%d", uid)
            return _err(str(exc), 500)

    async def ai_comment_submit(request: web.Request) -> web.Response:
        """AI Commenting: контекстные LLM-комментарии под постами целевых каналов.
        body: {channels: [ref...], niche?, tone?, acc_count?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        raw_ch = body.get("channels") or []
        if isinstance(raw_ch, str):
            raw_ch = re.split(r"[\s,]+", raw_ch)
        channels = [str(c).strip().lstrip("@") for c in raw_ch if str(c).strip()][:50]
        niche = validate_string(body.get("niche"), max_len=200) or ""
        from services.ai_comment_engine import COMMENT_TONES
        tone = (body.get("tone") or "neutral").strip()
        if tone not in COMMENT_TONES:
            tone = "neutral"
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 3, min_val=1, max_val=10) or 3, 10))
        except (TypeError, ValueError):
            acc_count = 3
        if not channels:
            return _err("Укажите хотя бы один канал", 400)
        from services import content_safety
        _v = await content_safety.enforce(pool, uid, niche or "comment", "", surface="ai_comment")
        if _v.blocked:
            return _err("Контент заблокирован политикой платформы", 403)
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"AI-комментинг: {len(channels)} каналов"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'ai_comment','pending',$2,$3,$4) RETURNING id",
                uid,
                _json.dumps({"channels": channels, "niche": niche, "tone": tone, "acc_count": acc_count}),
                len(channels), label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "channels": len(channels)})
        except Exception as exc:
            log.exception("ai_comment_submit uid=%d", uid)
            return _err(str(exc), 500)

    async def compliance_scan_submit(request: web.Request) -> web.Response:
        """Resource Compliance Scan: read-only проверка ресурсов на запрещённую
        тематику (CSAM/террор). Собирает досье, ничего не постит/не сносит.
        body: {resources: [ref...], per_resource_limit?, acc_count?}"""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        raw = body.get("resources") or []
        if isinstance(raw, str):
            raw = re.split(r"[\s,]+", raw)
        resources = [str(c).strip().lstrip("@") for c in raw if str(c).strip()][:100]
        if not resources:
            return _err("Укажите хотя бы один ресурс для проверки", 400)
        try:
            per_limit = max(1, min(validate_integer(body.get("per_resource_limit") or 50, min_val=1, max_val=200) or 50, 200))
        except (TypeError, ValueError):
            per_limit = 50
        try:
            acc_count = max(1, min(validate_integer(body.get("acc_count") or 2, min_val=1, max_val=10) or 2, 10))
        except (TypeError, ValueError):
            acc_count = 2
        has_acc = await _safe_count(pool,
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL",
            uid)
        if not has_acc:
            return _err("Нет активных аккаунтов", 400)
        try:
            label = f"Проверка на запрещёнку: {len(resources)} ресурсов"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'compliance_scan','pending',$2,$3,$4) RETURNING id",
                uid,
                _json.dumps({"resources": resources, "per_resource_limit": per_limit, "acc_count": acc_count}),
                len(resources), label)
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "resources": len(resources)})
        except Exception as exc:
            log.exception("compliance_scan_submit uid=%d", uid)
            return _err(str(exc), 500)

    async def rotate_proxies(request: web.Request) -> web.Response:
        """Безопасная ротация назначений прокси по пулу (anti-detection), без потери
        изоляции. Меняет только proxy_id простаивающих аккаунтов внутри транзакции с
        FOR UPDATE; резолвер не трогаем. body: {account_ids?: [], proxy_ids?: []}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        acc_ids = [int(x) for x in (body.get("account_ids") or []) if str(x).strip().isdigit()]
        pool_ids_in = [int(x) for x in (body.get("proxy_ids") or []) if str(x).strip().isdigit()]
        from services import proxy_rotation
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Блокируем строки ротируемых аккаунтов (busy-safe против claim).
                    if acc_ids:
                        accs = await conn.fetch(
                            "SELECT id, proxy_id, COALESCE(in_operation,FALSE) AS busy "
                            "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
                            "AND id = ANY($2::bigint[]) FOR UPDATE",
                            uid, acc_ids)
                    else:
                        accs = await conn.fetch(
                            "SELECT id, proxy_id, COALESCE(in_operation,FALSE) AS busy "
                            "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE FOR UPDATE",
                            uid)
                    group_ids = [int(r["id"]) for r in accs]
                    free = [{"account_id": int(r["id"]), "proxy_id": r["proxy_id"]}
                            for r in accs if not r["busy"]]
                    skipped_busy = len(accs) - len(free)
                    free_ids = [a["account_id"] for a in free]

                    # 2. Пул: владелец + активные; вычесть прокси, занятые ЛЮБЫМ
                    # не-ротируемым аккаунтом (busy в группе ИЛИ вне группы) — иначе
                    # два аккаунта окажутся на одном прокси (потеря изоляции).
                    if pool_ids_in:
                        prows = await conn.fetch(
                            "SELECT id FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE "
                            "AND id = ANY($2::int[])", uid, pool_ids_in)
                    else:
                        prows = await conn.fetch(
                            "SELECT id FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE", uid)
                    pool_available = [int(r["id"]) for r in prows]
                    blocked_rows = await conn.fetch(
                        "SELECT DISTINCT proxy_id FROM tg_accounts "
                        "WHERE owner_id=$1 AND proxy_id = ANY($2::int[]) "
                        "AND NOT (id = ANY($3::bigint[]))",
                        uid, pool_available, free_ids or [0])
                    blocked = {int(r["proxy_id"]) for r in blocked_rows if r["proxy_id"] is not None}
                    pool_final = [p for p in pool_available if p not in blocked]

                    # 3. План (чистый, инъективный) + 4. применение с гвардом busy.
                    result = proxy_rotation.plan_rotation(free, pool_final)
                    applied = 0
                    for ch in result["plan"]:
                        if ch["new"] == ch["old"]:
                            continue
                        res = await conn.execute(
                            "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3 "
                            "AND COALESCE(in_operation,FALSE)=FALSE",
                            ch["new"], ch["account_id"], uid)
                        if isinstance(res, str) and res.endswith(" 1"):
                            applied += 1
            return _json_resp({
                "ok": True, "rotated": applied,
                "skipped_busy": skipped_busy,
                "skipped_no_proxy": result["skipped_no_proxy"],
                "accounts": len(group_ids), "pool": len(pool_final),
            })
        except Exception as exc:
            log.exception("rotate_proxies uid=%d", uid)
            return _err(str(exc)[:150], 500)

    async def reporter_submit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        target = validate_string(body.get("target"), max_len=200)
        reason = validate_string(body.get("reason"), max_len=50) or "spam"
        acc_count = validate_integer(body.get("acc_count", 5), min_val=1, max_val=100) or 5
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
        text = validate_string(body.get("text"), max_len=4096)
        channel_ids = body.get("channel_ids") or []
        if not text:
            return _err("Заполните текст поста", 400)
        if check_sql_suspicious(text):
            return _err("Invalid characters in post text")
        if not channel_ids:
            return _err("Выберите хотя бы один канал", 400)
        # Validate channel IDs are integers
        try:
            channel_ids = [int(x) for x in channel_ids if str(x).lstrip("-").isdigit()]
        except (TypeError, ValueError):
            return _err("Invalid channel IDs", 400)
        try:
            schedule_minutes = max(0, min(validate_integer(body.get("schedule_minutes") or 0, min_val=0) or 0, 60 * 24 * 30))
        except (TypeError, ValueError):
            schedule_minutes = 0
        # Autopost v2: рекуррентная публикация (0 = разовая).
        try:
            _rmin = max(0, min(int(body.get("repeat_interval_min") or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            _rmin = 0
        try:
            label = f"Quick Post в {len(channel_ids)} каналов"
            _pd = {"text": text, "channel_ids": channel_ids}
            if _rmin > 0:
                _pd["repeat_interval_min"] = _rmin
                _rc = body.get("repeat_count")
                if _rc is not None:
                    try:
                        _pd["repeat_count"] = max(1, min(int(_rc), 1000))
                    except (TypeError, ValueError):
                        pass
            _params = _json.dumps(_pd)
            if schedule_minutes > 0:
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label, scheduled_for) "
                    "VALUES($1,'quick_post','pending',$2,$3,$4, now() + ($5 || ' minutes')::interval) RETURNING id",
                    uid, _params, len(channel_ids), "⏰ " + label, str(schedule_minutes),
                )
            else:
                op_id = await pool.fetchval(
                    "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                    "VALUES($1,'quick_post','pending',$2,$3,$4) RETURNING id",
                    uid, _params, len(channel_ids), label,
                )
            return _json_resp({"ok": True, "op_id": op_id, "label": label, "scheduled_minutes": schedule_minutes})
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
            # Рекомендации по переоптимизации БОТОВ (авто-сгенерированы при падении
            # позиции; применяются в один клик через /api/miniapp/seo/apply_bot).
            bot_suggestions = await _safe_fetch(pool,
                """SELECT bs.bot_id, bs.name, bs.short_desc, bs.reason, bs.keyword,
                          bs.created_at, bs.applied_at, mb.username AS bot_username
                   FROM bot_seo_suggestions bs
                   LEFT JOIN managed_bots mb ON mb.bot_id=bs.bot_id
                   WHERE bs.owner_id=$1 ORDER BY bs.created_at DESC LIMIT 20""",
                uid) or []
            return _json_resp({
                "suggestions": [
                    {**dict(s), "created_at": s["created_at"].isoformat() if s["created_at"] else None}
                    for s in suggestions
                ],
                "bot_suggestions": [
                    {**dict(b),
                     "created_at": b["created_at"].isoformat() if b["created_at"] else None,
                     "applied_at": b["applied_at"].isoformat() if b["applied_at"] else None}
                    for b in bot_suggestions
                ],
                "keywords": [dict(k) for k in keywords],
            })
        except Exception as exc:
            log.exception("seo_overview uid=%d", uid)
            return _err(str(exc), 500)

    async def seo_apply(request: web.Request) -> web.Response:
        """Применить AI SEO-предложение к каналу в один клик (замыкает петлю
        анализ→рекомендация→ПРИМЕНЕНИЕ). Раньше предложения (title/about/username)
        только показывались — оптимизацию приходилось вбивать вручную.

        Переиспользует существующее: seo_ai_suggestions (хранимая рекомендация),
        managed_channels.acc_id → управляющий аккаунт, account_manager.edit_channel_*.
        body: {chan_id, fields?: ["title","about","username"]} — по умолчанию все.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad json", 400)
        try:
            chan_id = int(body.get("chan_id"))
        except (TypeError, ValueError):
            return _err("chan_id обязателен", 400)
        fields = body.get("fields") or None

        # Единая реализация применения (та же, что в bulk-исполнителе) — без дублей.
        from services.seo_apply import apply_seo_to_channel
        try:
            res = await apply_seo_to_channel(pool, uid, chan_id, fields)
        except Exception as exc:
            log.exception("seo_apply uid=%d chan=%d", uid, chan_id)
            return _err(str(exc)[:200], 500)

        if res.get("ok"):
            return _json_resp(res)
        return _err(res.get("error") or "Не удалось применить", 400)

    async def seo_apply_all(request: web.Request) -> web.Response:
        """Массовое применение SEO-предложений по ВСЕЙ сетке — в очередь (op_worker),
        т.к. правок много и инлайн упрётся в таймаут шлюза."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        # Каналы владельца, у которых есть хотя бы одно SEO-предложение и
        # управляющий аккаунт.
        rows = await _safe_fetch(pool,
            "SELECT DISTINCT mc.channel_id FROM managed_channels mc "
            "WHERE mc.owner_id=$1 AND mc.acc_id IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM seo_ai_suggestions s "
            "            WHERE s.owner_id=$1 AND s.chan_id=mc.channel_id)",
            uid)
        chan_ids = [int(r["channel_id"]) for r in (rows or [])]
        if not chan_ids:
            return _err("Нет каналов с SEO-предложениями для применения", 400)
        op_id = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'bulk_seo_apply','pending',$2,$3,$4) RETURNING id",
            uid, _json.dumps({"channel_ids": chan_ids}), len(chan_ids),
            f"SEO по сетке: {len(chan_ids)} каналов")
        return _json_resp({"ok": True, "op_id": op_id, "count": len(chan_ids)})

    async def seo_apply_bot(request: web.Request) -> web.Response:
        """Применить рекомендацию по переоптимизации БОТА в один клик (замыкает
        петлю анализ→рекомендация→применение для стороны ботов; аналог
        /seo/apply для каналов). Рекомендация авто-генерируется при падении
        позиции (ranking_checker, opt-in). body: {bot_id, fields?}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad json", 400)
        try:
            bot_id = int(body.get("bot_id"))
        except (TypeError, ValueError):
            return _err("bot_id обязателен", 400)
        fields = body.get("fields") or None
        from services import bot_reoptimizer
        try:
            res = await bot_reoptimizer.apply_bot_seo(pool, uid, bot_id, fields)
        except Exception as exc:
            log.exception("seo_apply_bot uid=%d bot=%d", uid, bot_id)
            return _err(str(exc)[:200], 500)
        if res.get("ok"):
            return _json_resp(res)
        return _err(res.get("error") or "Не удалось применить", 400)

    async def reopt_setting(request: web.Request) -> web.Response:
        """Тумблер авто-переоптимизации ботов при падении позиции (opt-in).
        body: {enabled: bool}. Хранится в visibility_alert_settings.auto_reoptimize."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("bad json", 400)
        enabled = bool(body.get("enabled"))
        try:
            await pool.execute(
                """INSERT INTO visibility_alert_settings(owner_id, auto_reoptimize, alerts_enabled)
                   VALUES($1,$2,TRUE)
                   ON CONFLICT(owner_id) DO UPDATE SET auto_reoptimize=$2""",
                uid, enabled)
        except Exception as exc:
            log.exception("reopt_setting uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({"ok": True, "enabled": enabled})

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
        token = validate_string(data.get("token"), max_len=200)
        if not token:
            return _err("token обязателен", 400)
        if not validate_bot_token(token):
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

    async def bot_factory_create(request: web.Request) -> web.Response:
        """Создание ботов со всеми настройками из Mini App."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        token = validate_string(data.get("token"), max_len=200)
        name_template = validate_string(data.get("name"), max_len=128)
        uname_template = validate_string(data.get("username"), max_len=32)
        description = validate_string(data.get("description"), max_len=512)
        short_desc = validate_string(data.get("short_description"), max_len=120)
        account_id = data.get("account_id")
        count = min(max(validate_integer(data.get("count", 1), min_val=1, max_val=10) or 1, 1), 10)
        ecosystem_id = data.get("ecosystem_id")
        if not token:
            return _err("token обязателен", 400)
        if not name_template:
            return _err("имя бота обязательно", 400)
        if not validate_bot_token(token):
            return _err("Неверный формат токена", 400)
        try:
            import aiohttp as _aio
            async with _aio.ClientSession() as _http:
                async with _http.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=_aio.ClientTimeout(total=10),
                ) as _resp:
                    me = await _resp.json()
            if not me.get("ok"):
                return _err(f"Telegram API: {me.get('description', 'неверный токен')}", 400)
            bot_info = me["result"]
            bot_id = bot_info["id"]
            from database import db as _db
            already_owned = await _safe_count(pool,
                "SELECT COUNT(*) FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, uid)
            if not already_owned:
                from bot.utils.subscription import get_bot_limit, get_effective_bot_count
                _lim = await get_bot_limit(pool, uid)
                if await get_effective_bot_count(pool, uid) >= _lim:
                    return _err(f"Достигнут лимит ботов ({_lim})", 403)
            result = await _db.add_bot(pool, token, bot_id, bot_info.get("username", ""), bot_info.get("first_name", ""), uid)
            if result == "taken":
                return _err("Этот бот уже добавлен другим пользователем", 409)
            if result is False:
                return _json_resp({"ok": True, "already_exists": True, "bot_id": bot_id})
            # Применяем настройки к боту через Telegram API
            applied = []
            if name_template:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyName?name={_aio.helpers.quote(name_template)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("name")
                except Exception as e:
                    log.warning("bot_factory_create setMyName: %s", e)
            if description:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyDescription?description={_aio.helpers.quote(description)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("description")
                except Exception as e:
                    log.warning("bot_factory_create setMyDescription: %s", e)
            if short_desc:
                try:
                    async with _aio.ClientSession() as _http:
                        await _http.get(f"https://api.telegram.org/bot{token}/setMyShortDescription?short_description={_aio.helpers.quote(short_desc)}", timeout=_aio.ClientTimeout(total=10))
                    applied.append("short_description")
                except Exception as e:
                    log.warning("bot_factory_create setMyShortDescription: %s", e)
            # Привязка к экосистеме
            if ecosystem_id:
                try:
                    await pool.execute(
                        "INSERT INTO ecosystem_bots (ecosystem_id, bot_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        int(ecosystem_id), bot_id,
                    )
                    applied.append("ecosystem")
                except Exception as e:
                    log.warning("bot_factory_create ecosystem link: %s", e)
            return _json_resp({
                "ok": True, "bot_id": bot_id,
                "username": bot_info.get("username", ""),
                "first_name": bot_info.get("first_name", ""),
                "applied_settings": applied,
                "count": count,
            })
        except Exception as exc:
            log.exception("bot_factory_create uid=%d", uid)
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
        name = validate_string(body.get("persona_name"), max_len=100)
        if not name:
            return _err("Имя персоны обязательно", 400)
        VALID_STYLES = {"formal", "casual", "expert", "friendly", "sarcastic", "neutral"}
        speech_style = validate_string(body.get("speech_style"), max_len=20) or "casual"
        if speech_style not in VALID_STYLES:
            speech_style = "casual"
        try:
            interests_raw = body.get("interests", "")
            if isinstance(interests_raw, list):
                interests = [validate_string(i, max_len=50) or "" for i in interests_raw]
                interests = [i for i in interests if i]
            else:
                interests = [validate_string(t, max_len=50) or "" for t in str(interests_raw).split(",")]
                interests = [i for i in interests if i]
            age = validate_integer(body.get("age") or 25, min_val=18, max_val=80) or 25
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
        try:
            count = max(1, min(50, validate_integer(body.get("count") or 1, min_val=1, max_val=50) or 1))
        except (TypeError, ValueError):
            return _err("Некорректное количество", 400)
        country = validate_string(body.get("country"), max_len=4) or "RU"
        # Проверяем, что SMS-сервис настроен (иначе операция гарантированно упадёт)
        try:
            from bot.handlers.auto_registrar import _get_sms_client
            _client, _service = await _get_sms_client(pool)
        except Exception:
            _client, _service = None, "?"
        if not _client:
            return _err(f"SMS-сервис не настроен ({_service}). Обратитесь к администратору платформы.", 400)
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'auto_register','pending',$2,$3,$4) RETURNING id",
                uid, _json.dumps({"count": count, "country": country}), count,
                f"Авторег {count} акк. ({country})",
            )
            return _json_resp({"ok": True, "op_id": op_id, "count": count})
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
        phones_raw = validate_string(body.get("phones"), max_len=10000)
        if not phones_raw:
            return _err("Укажите номера телефонов", 400)
        phones = [p.strip() for p in phones_raw.replace(",", "\n").split("\n") if p.strip()]
        # Validate phone format (digits, spaces, dashes, plus)
        phones = [re.sub(r'[^\d+\-() ]', '', p) for p in phones]
        phones = [p for p in phones if len(p) >= 5]
        if not phones:
            return _err("Нет валидных номеров", 400)
        # Limit batch size
        phones = phones[:500]
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
        if source not in ("parsed", "crm", "bot_users", "import_list"):
            return _err("Неизвестный источник аудитории", 400)
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
        # Свой список: разбираем вставленный текст на user_refs (@username/id) и
        # phones общими парсерами движка — раньше UI давал только 3 таблицы-источника.
        user_refs: list = []
        phones: list = []
        if source == "import_list":
            import re as _re_il
            from services.mass_inviter_engine import parse_user_refs, parse_phones
            raw = body.get("import_list") or ""
            # Взаимоисключающий разбор: токен с '+' → телефон, иначе @username/ID.
            # Иначе числовой ID попал бы и в refs, и в phones (двойной инвайт).
            _tokens = [t for t in _re_il.split(r"[,;\s\n]+", str(raw).strip()) if t]
            phones = parse_phones(" ".join(t for t in _tokens if t.startswith("+")))
            user_refs = parse_user_refs(" ".join(t for t in _tokens if not t.startswith("+")))
            if not user_refs and not phones:
                return _err("Список пуст или невалиден", 400)
        # Настройки безопасности/темпа.
        pace = (body.get("pace") or "normal").strip()
        if pace not in ("slow", "normal", "fast"):
            pace = "normal"
        def _clamp(v, lo, hi, d):
            try:
                return max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                return d
        batch_size = _clamp(body.get("batch_size"), 1, 20, 5) if body.get("batch_size") is not None else 5
        max_invites = _clamp(body.get("max_invites"), 0, 100000, 0) if body.get("max_invites") is not None else 0
        per_account_limit = _clamp(body.get("per_account_limit"), 0, 10000, 0) if body.get("per_account_limit") is not None else 0
        try:
            label = f"Mass Invite → {group}"
            params = {"group": group, "source": source, "pace": pace, "batch_size": batch_size}
            if user_refs:
                params["user_refs"] = user_refs
            if phones:
                params["phones"] = phones
            if max_invites:
                params["max_invites"] = max_invites
            if per_account_limit:
                params["per_account_limit"] = per_account_limit
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
        # Полный список типов таргета, поддержанных dm_engine._get_targets.
        _ALLOWED_TARGETS = {
            "all_bots", "bot_users", "cohort", "crm", "parsed_audience", "import_list",
        }
        if target_type not in _ALLOWED_TARGETS:
            return _err(f"target_type должен быть одним из: {', '.join(sorted(_ALLOWED_TARGETS))}", 400)
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
        if target_type == "cohort" and not target_id:
            return _err("Для когорты выберите бота", 400)
        # Когорта по активности (совпадает с dm_engine._get_targets).
        _COHORT_SQL = {
            "hot":  "ua.last_seen >= now() - INTERVAL '1 day'",
            "warm": "ua.last_seen >= now() - INTERVAL '7 days' AND ua.last_seen < now() - INTERVAL '1 day'",
            "cold": "ua.last_seen >= now() - INTERVAL '30 days' AND ua.last_seen < now() - INTERVAL '7 days'",
            "lost": "ua.last_seen < now() - INTERVAL '30 days'",
        }
        cohort_type = None
        if target_type == "cohort":
            cohort_type = (body.get("cohort_type") or "warm").strip()
            if cohort_type not in _COHORT_SQL:
                cohort_type = "warm"
        # import_list: свой список получателей (user_id / @username), разбор общим
        # хелпером dm_engine — раньше этот тип таргета движок умел, но UI не давал.
        import_list = []
        if target_type == "import_list":
            from services.dm_engine import parse_import_list
            import_list = parse_import_list(body.get("import_list"))
            if not import_list:
                return _err("Список получателей пуст или невалиден", 400)
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
            elif target_type == "cohort" and target_id:
                total_targets = await _safe_count(pool,
                    f"""SELECT COUNT(*) FROM user_activity ua
                        JOIN managed_bots mb ON mb.bot_id=ua.bot_id AND mb.added_by=$2
                        WHERE ua.bot_id=$1 AND {_COHORT_SQL[cohort_type]}""",
                    int(target_id), uid)
            elif target_type == "crm":
                total_targets = await _safe_count(pool,
                    "SELECT COUNT(*) FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0", uid)
            elif target_type == "parsed_audience":
                if target_id:
                    total_targets = await _safe_count(pool,
                        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2 AND tg_user_id > 0",
                        uid, int(target_id))
                else:
                    total_targets = await _safe_count(pool,
                        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1 AND tg_user_id > 0", uid)
            elif target_type == "import_list":
                total_targets = len(import_list)
        except Exception:
            total_targets = 0
        # Темп рассылки (params.pace): slow безопаснее, fast быстрее (риск бана).
        pace = (body.get("pace") or "normal").strip()
        if pace not in ("slow", "normal", "fast"):
            pace = "normal"
        _params = {"pace": pace}
        if cohort_type:
            _params["cohort_type"] = cohort_type
        if import_list:
            _params["import_list"] = import_list
        # Медиа (фото/видео/док по URL) — send_dm отправит как файл с подписью.
        media_url = (body.get("media_url") or "").strip()
        if media_url:
            if not is_safe_public_url(media_url):
                return _err("Нужен публичный https URL медиа", 400)
            _params["media_url"] = media_url
        # Дневной лимит отправок на аккаунт (защита от бана). Клампим 1..200.
        if body.get("per_account_daily") is not None:
            try:
                _pad = int(body["per_account_daily"])
                if _pad > 0:
                    _params["per_account_daily"] = max(1, min(200, _pad))
            except (TypeError, ValueError):
                return _err("per_account_daily должен быть числом", 400)
        params_json = _json.dumps(_params)
        try:
            try:
                row = await pool.fetchrow(
                    """INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, total_targets, params)
                       VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id""",
                    uid, name, text, target_type, int(target_id) if target_id else None, total_targets, params_json,
                )
            except asyncpg.UndefinedColumnError:
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
        if plan_type not in ("gentle", "standard", "aggressive"):
            return _err("plan_type должен быть gentle/standard/aggressive", 400)
        if not account_id:
            return _err("account_id обязателен", 400)

        # ── Расширенные настройки поведения прогрева (движок читает их из
        # account_niche_profiles) — раньше не выводились в UI, аккаунт всегда
        # грелся как mixed/general. Теперь пользователь задаёт характер. ──
        from services.account_warmer import (
            WARMUP_PROFILES, WARMUP_NICHES, normalize_warmup_channels,
        )
        profile_type = (body.get("profile_type") or "mixed").strip().lower()
        if profile_type not in WARMUP_PROFILES:
            return _err(f"profile_type должен быть одним из: {', '.join(WARMUP_PROFILES)}", 400)
        niche = (body.get("niche") or "general").strip().lower()
        if niche not in WARMUP_NICHES:
            return _err(f"niche должен быть одним из: {', '.join(WARMUP_NICHES)}", 400)
        custom_channels = normalize_warmup_channels(body.get("custom_channels"))

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
            # Опциональные переопределения (продвинутый режим). Клампим по
            # безопасности: >20 действий/день на свежем аккаунте — топ-триггер
            # бана, поэтому жёсткий потолок независимо от ввода пользователя.
            if body.get("daily_actions") is not None:
                try:
                    daily_actions = max(3, min(20, int(body["daily_actions"])))
                except (TypeError, ValueError):
                    return _err("daily_actions должен быть числом 3..20", 400)
            if body.get("target_days") is not None:
                try:
                    target_days = max(5, min(45, int(body["target_days"])))
                except (TypeError, ValueError):
                    return _err("target_days должен быть числом 5..45", 400)

            # Сохраняем поведенческий профиль — движок прогрева читает именно
            # отсюда (profile_type → веса действий, niche/custom_channels → на
            # каких каналах греть).
            await pool.execute(
                """CREATE TABLE IF NOT EXISTS account_niche_profiles (
                       account_id      BIGINT PRIMARY KEY,
                       owner_id        BIGINT NOT NULL,
                       niche           TEXT NOT NULL DEFAULT 'general',
                       profile_type    TEXT NOT NULL DEFAULT 'reader',
                       custom_channels TEXT[] DEFAULT '{}',
                       flood_wait_count INT NOT NULL DEFAULT 0,
                       last_flood_at   TIMESTAMPTZ,
                       updated_at      TIMESTAMPTZ DEFAULT NOW()
                   )"""
            )
            await pool.execute(
                """INSERT INTO account_niche_profiles(account_id, owner_id, niche, profile_type, custom_channels, updated_at)
                   VALUES($1,$2,$3,$4,$5,now())
                   ON CONFLICT (account_id) DO UPDATE
                   SET niche=$3, profile_type=$4, custom_channels=$5, updated_at=now()""",
                account_id, uid, niche, profile_type, custom_channels,
            )

            label = f"Прогрев аккаунта ({plan_type}, {profile_type})"
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'account_warmup','pending',$2,1,$3) RETURNING id",
                uid, _json.dumps({
                    "account_id": account_id, "plan_type": plan_type,
                    "profile_type": profile_type, "niche": niche,
                }), label,
            )
            await pool.execute(
                """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT (account_id) DO UPDATE
                   SET plan_type=$3, target_days=$4, daily_actions=$5, status='active', started_at=now()""",
                uid, account_id, plan_type, target_days, daily_actions,
            )
            return _json_resp({
                "ok": True, "op_id": op_id, "label": label,
                "profile_type": profile_type, "niche": niche,
                "custom_channels": len(custom_channels),
                "daily_actions": daily_actions, "target_days": target_days,
            })
        except Exception as exc:
            log.exception("warmup_create_plan uid=%d acc=%s", uid, account_id)
            return _err(str(exc), 500)

    async def warmup_bulk_start(request: web.Request) -> web.Response:
        """Массовый прогрев: создать планы для ВСЕХ подходящих аккаунтов сразу
        (активные, с сессией, ещё без активного плана). Раньше прогрев запускался
        строго по одному аккаунту — масс-действие отсутствовало. Фоновый
        run_warmup_loop подхватит новые active-планы, отдельные op не нужны."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from services.account_warmer import WARMUP_PROFILES, WARMUP_NICHES
        plan_type = (body.get("plan_type") or "standard").strip()
        if plan_type not in ("gentle", "standard", "aggressive"):
            plan_type = "standard"
        profile_type = (body.get("profile_type") or "mixed").strip().lower()
        if profile_type not in WARMUP_PROFILES:
            profile_type = "mixed"
        niche = (body.get("niche") or "general").strip().lower()
        if niche not in WARMUP_NICHES:
            niche = "general"
        days_map = {"gentle": 21, "standard": 14, "aggressive": 10}
        actions_map = {"gentle": 5, "standard": 10, "aggressive": 12}
        target_days = days_map[plan_type]
        daily_actions = actions_map[plan_type]

        # Подходящие: активные, с сессией, без уже активного плана прогрева.
        rows = await _safe_fetch(pool,
            """SELECT a.id FROM tg_accounts a
               WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.session_str IS NOT NULL
                 AND COALESCE(a.acc_status,'active') NOT IN ('banned','deactivated','session_expired')
                 AND NOT EXISTS (
                     SELECT 1 FROM account_warmup_plans wp
                     WHERE wp.account_id=a.id AND wp.status='active')
               LIMIT 500""", uid)
        acc_ids = [int(r["id"]) for r in (rows or [])]
        if not acc_ids:
            return _json_resp({"ok": True, "started": 0, "note": "Нет подходящих аккаунтов (все уже греются или недоступны)"})
        started = 0
        try:
            await pool.execute(
                """CREATE TABLE IF NOT EXISTS account_niche_profiles (
                       account_id BIGINT PRIMARY KEY, owner_id BIGINT NOT NULL,
                       niche TEXT NOT NULL DEFAULT 'general', profile_type TEXT NOT NULL DEFAULT 'reader',
                       custom_channels TEXT[] DEFAULT '{}', flood_wait_count INT NOT NULL DEFAULT 0,
                       last_flood_at TIMESTAMPTZ, updated_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            for acc_id in acc_ids:
                await pool.execute(
                    """INSERT INTO account_niche_profiles(account_id, owner_id, niche, profile_type, updated_at)
                       VALUES($1,$2,$3,$4,now())
                       ON CONFLICT (account_id) DO UPDATE SET niche=$3, profile_type=$4, updated_at=now()""",
                    acc_id, uid, niche, profile_type)
                await pool.execute(
                    """INSERT INTO account_warmup_plans(owner_id, account_id, plan_type, target_days, daily_actions)
                       VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT (account_id) DO UPDATE
                       SET plan_type=$3, target_days=$4, daily_actions=$5, status='active', started_at=now()""",
                    uid, acc_id, plan_type, target_days, daily_actions)
                started += 1
            return _json_resp({"ok": True, "started": started, "plan_type": plan_type,
                               "profile_type": profile_type, "niche": niche})
        except Exception as exc:
            log.exception("warmup_bulk_start uid=%d", uid)
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

    async def warmup_pause_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("bad plan_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans SET status='paused'
                   WHERE id=$1 AND owner_id=$2 AND status='active'
                   RETURNING id""",
                plan_id, uid)
            if not row:
                return _err("Активный план не найден", 404)
            return _json_resp({"ok": True, "status": "paused"})
        except Exception as exc:
            log.exception("warmup_pause_plan uid=%d plan=%d", uid, plan_id)
            return _err(str(exc), 500)

    async def warmup_resume_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("bad plan_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans SET status='active'
                   WHERE id=$1 AND owner_id=$2 AND status='paused'
                   RETURNING id, account_id""",
                plan_id, uid)
            if not row:
                return _err("Приостановленный план не найден", 404)
            try:
                await pool.execute(
                    """UPDATE tg_accounts SET acc_status='warming'
                       WHERE id=$1 AND is_active=TRUE
                         AND COALESCE(acc_status,'active')='active'""",
                    row["account_id"])
            except Exception:
                log.warning("warmup_resume_plan: acc_status update failed plan=%d", plan_id)
            return _json_resp({"ok": True, "status": "active"})
        except Exception as exc:
            log.exception("warmup_resume_plan uid=%d plan=%d", uid, plan_id)
            return _err(str(exc), 500)

    async def warmup_cancel_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            plan_id = int(request.match_info["plan_id"])
        except (KeyError, ValueError):
            return _err("bad plan_id", 400)
        try:
            row = await pool.fetchrow(
                """UPDATE account_warmup_plans
                   SET status='cancelled', completed_at=NOW()
                   WHERE id=$1 AND owner_id=$2
                     AND status IN ('active','paused')
                   RETURNING id, account_id""",
                plan_id, uid)
            if not row:
                return _err("Активный план не найден", 404)
            try:
                await pool.execute(
                    """UPDATE tg_accounts SET acc_status='active'
                       WHERE id=$1 AND COALESCE(acc_status,'active')='warming'""",
                    row["account_id"])
            except Exception:
                log.warning("warmup_cancel_plan: acc_status reset failed plan=%d", plan_id)
            return _json_resp({"ok": True, "status": "cancelled"})
        except Exception as exc:
            log.exception("warmup_cancel_plan uid=%d plan=%d", uid, plan_id)
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
                    except Exception as e:
                        log.warning("multigeo_get get locale %s: %s", lc, e)
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
            # В работе: strike-операции из очереди (pending/running) — есть прогресс,
            # но ещё нет итоговых метрик.
            active = await _safe_fetch(pool,
                """SELECT id, COALESCE(label, op_type) AS label, status,
                          total_items, done_items, created_at
                   FROM operation_queue
                   WHERE owner_id=$1 AND op_type LIKE 'strike%'
                     AND status IN ('pending','running')
                   ORDER BY created_at DESC LIMIT 15""",
                uid)
            # Завершённые: из таблицы strike_history — с РЕАЛЬНЫМИ метриками
            # эффективности (жалобы, забанен ли таргет). Раньше endpoint их не читал,
            # поэтому UI показывал только «N/N» и Strike казался «неэффективным».
            done = await _safe_fetch(pool,
                """SELECT id, target, reason, accounts_used, peer_reported,
                          msgs_reported, pinned_reported, admins_reported,
                          network_reports, blocked, verified_down, spambot_escalation,
                          duration_s, created_at
                   FROM strike_history
                   WHERE owner_id=$1
                   ORDER BY created_at DESC LIMIT 30""",
                uid)
        except Exception as exc:
            log.exception("strike_history uid=%d", uid)
            return _err(str(exc), 500)

        operations = []
        for r in (active or []):
            operations.append({
                "id": r["id"],
                "label": r["label"] or "Strike",
                "status": r["status"] or "pending",
                "total": r["total_items"] or 0,
                "processed": r["done_items"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "in_progress": True,
            })
        for r in (done or []):
            # Суммарное число доставленных жалоб по всем векторам.
            reports = (int(r["peer_reported"] or 0) + int(r["msgs_reported"] or 0)
                       + int(r["pinned_reported"] or 0) + int(r["admins_reported"] or 0)
                       + int(r["network_reports"] or 0))
            operations.append({
                "id": f"h{r['id']}",
                "label": f"Strike: {r['target']}" + (f" [{r['reason']}]" if r["reason"] else ""),
                "status": "done",
                "target": r["target"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "in_progress": False,
                "metrics": {
                    "accounts_used": int(r["accounts_used"] or 0),
                    "reports_total": reports,
                    "peer_reported": int(r["peer_reported"] or 0),
                    "msgs_reported": int(r["msgs_reported"] or 0),
                    "admins_reported": int(r["admins_reported"] or 0),
                    "network_reports": int(r["network_reports"] or 0),
                    "blocked": bool(r["blocked"]),
                    "verified_down": bool(r["verified_down"]),
                    "spambot_escalation": bool(r["spambot_escalation"]),
                    "duration_s": int(r["duration_s"] or 0),
                },
            })
        # Сортируем объединённый список по времени (свежие сверху).
        operations.sort(key=lambda o: o["created_at"] or "", reverse=True)
        return _json_resp({"operations": operations[:40]})

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

            # Настройки интенсивности (раньше UI слал только target+category):
            # num_waves — эшелонирование (движок читает из params), max_accounts —
            # сколько аккаунтов задействовать (меньше = щадящее, дольше живут).
            try:
                num_waves = max(1, min(5, int(body.get("num_waves") or 3)))
            except (TypeError, ValueError):
                num_waves = 3
            try:
                max_accounts = int(body.get("max_accounts") or 0)
            except (TypeError, ValueError):
                max_accounts = 0
            acc_limit = max_accounts if 0 < max_accounts <= 50 else 50

            # Подсчёт доступных аккаунтов
            accs = await pool.fetch(
                """SELECT id FROM tg_accounts
                   WHERE owner_id=$1 AND is_active=true
                     AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired')
                   LIMIT $2""",
                uid, acc_limit,
            )
            if not accs:
                return _err("Нет доступных активных аккаунтов для Strike", 400)

            # Создаём операцию в очереди (op_type='strike' — воркер знает этот тип)
            op_id = await pool.fetchval(
                """INSERT INTO operation_queue(owner_id, op_type, label, status, params, total_items)
                   VALUES($1, 'strike', $2, 'pending', $3::jsonb, $4)
                   RETURNING id""",
                uid,
                f"Strike: {normalized} [{cat['label']}] · {num_waves} волн",
                __import__("json").dumps({
                    "target": normalized,
                    "reason": cat["tg_reason"],
                    "num_waves": num_waves,
                    "account_ids": [r["id"] for r in accs],
                }),
                len(accs),
            )
            return _json_resp({"ok": True, "operation_id": op_id, "accounts": len(accs), "num_waves": num_waves})
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
        q = request.rel_url.query
        try:
            limit = min(int(q.get("limit", "50")), 200)
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = int(q.get("offset", "0"))
        except (ValueError, TypeError):
            offset = 0
        filt_sql, filt_params = parsed_audience_filters(q, base_params_count=1)
        try:
            rows = await pool.fetch(
                f"""SELECT tg_user_id, username, first_name, last_name, is_premium,
                           is_bot, is_active, phone, source_title, source_type, parsed_at
                    FROM parsed_audiences WHERE owner_id=$1{filt_sql}
                    ORDER BY parsed_at DESC LIMIT ${len(filt_params)+2} OFFSET ${len(filt_params)+3}""",
                uid, *filt_params, limit, offset,
            )
            total = await pool.fetchval(
                f"SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1{filt_sql}",
                uid, *filt_params,
            )
            # Счётчики срезов (по всей аудитории владельца, без учёта фильтров) —
            # чтобы показать «сколько premium / с телефоном / активных» есть вообще.
            slices = await pool.fetchrow(
                """SELECT COUNT(*) FILTER (WHERE is_premium) AS premium,
                          COUNT(*) FILTER (WHERE username IS NOT NULL AND username<>'') AS with_username,
                          COUNT(*) FILTER (WHERE phone IS NOT NULL AND phone<>'') AS with_phone,
                          COUNT(*) FILTER (WHERE is_active) AS active
                   FROM parsed_audiences WHERE owner_id=$1""",
                uid,
            )
        except Exception as exc:
            log.exception("parsed_audience uid=%d", uid)
            return _err(str(exc), 500)
        return _json_resp({
            "total": int(total or 0),
            "slices": {k: int(slices[k] or 0) for k in ("premium", "with_username", "with_phone", "active")} if slices else {},
            "users": [
                {
                    "tg_user_id": r["tg_user_id"],
                    "username": r["username"] or "",
                    "name": " ".join(filter(None, [r["first_name"], r["last_name"]])),
                    "is_premium": bool(r["is_premium"]),
                    "is_bot": bool(r["is_bot"]),
                    "is_active": bool(r["is_active"]),
                    "has_phone": bool(r["phone"]),
                    "source_title": r["source_title"] or "",
                    "source_type": r["source_type"] or "",
                    "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else None,
                }
                for r in rows
            ],
        })

    async def parsed_audience_export(request: web.Request) -> web.Response:
        """Экспорт аудитории с теми же фильтрами, что и просмотр.
        ?format=csv|txt|json (по умолчанию csv). TXT — @username/id построчно
        (готово для инвайтера), JSON — полные объекты. Паритет Telegram Expert."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.rel_url.query
        fmt = (q.get("format") or "csv").strip().lower()
        if fmt not in ("csv", "txt", "json"):
            fmt = "csv"
        filt_sql, filt_params = parsed_audience_filters(q, base_params_count=1)
        try:
            rows = await pool.fetch(
                f"""SELECT tg_user_id, username, first_name, last_name, phone,
                           is_premium, is_bot, is_active, source_title, parsed_at
                    FROM parsed_audiences WHERE owner_id=$1{filt_sql}
                    ORDER BY parsed_at DESC LIMIT 50000""",
                uid, *filt_params,
            )
        except Exception as exc:
            log.exception("parsed_audience_export uid=%d", uid)
            return _err(str(exc), 500)
        if fmt == "csv":
            header = ["tg_user_id", "username", "first_name", "last_name", "phone",
                      "is_premium", "is_bot", "is_active", "source", "parsed_at"]
            data = [
                [r["tg_user_id"], r["username"] or "", r["first_name"] or "", r["last_name"] or "",
                 r["phone"] or "", r["is_premium"], r["is_bot"], r["is_active"],
                 r["source_title"] or "", r["parsed_at"].isoformat() if r["parsed_at"] else ""]
                for r in rows
            ]
            return _csv_resp("parsed_audience.csv", header, data)
        # txt / json — через чистые хелперы parser
        from services import parser as _parser
        users = [{
            "tg_user_id": r["tg_user_id"], "username": r["username"],
            "first_name": r["first_name"], "last_name": r["last_name"],
            "phone": r["phone"], "is_premium": r["is_premium"], "is_bot": r["is_bot"],
            "is_active": r["is_active"], "source_title": r["source_title"],
            "parsed_at": r["parsed_at"].isoformat() if r["parsed_at"] else "",
        } for r in rows]
        if fmt == "txt":
            body, ctype, fname = _parser.audience_to_txt(users), "text/plain", "parsed_audience.txt"
        else:
            body, ctype, fname = _parser.audience_to_json(users), "application/json", "parsed_audience.json"
        return web.Response(text=body, content_type=ctype, charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"',
                     "Access-Control-Allow-Origin": "*"})

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
        # days_back — окно активности для режима "active" (бэкенд умел, UI хардкодил 30).
        try:
            days_back = max(1, min(365, int(body.get("days_back") or 30)))
        except (TypeError, ValueError):
            days_back = 30
        import json as _json
        _win = f", {days_back}д" if parse_type in ("active", "comments") else ""
        label = f"Парсинг {parse_type} из @{source_ref} (до {limit}{_win})"
        try:
            op_id = await pool.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
                "VALUES($1,'parse_audience','pending',$2,$3,$4) RETURNING id",
                uid,
                _json.dumps({"source_ref": source_ref, "parse_type": parse_type,
                             "limit": limit, "days_back": days_back}),
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
                "SELECT id, keyword, status, target_position, current_subs, target_subs, "
                "bot_id, smm_panel_id, created_at "
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
                    "bot_id": r["bot_id"],
                    "smm_panel_id": r["smm_panel_id"],
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

    async def promo_order_boost(request: web.Request) -> web.Response:
        """Отправить заказ в SMM-панель — то же самое, что кнопка «🚀 Накрутить»
        в боте (promo_platform.py:cb_order_boost). Раньше в mini_app не было
        способа привязать заказ к боту/панели или запустить накрутку — заказ,
        созданный тут, навсегда оставался в статусе 'waiting'."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            order_id = int(request.match_info["order_id"])
        except (KeyError, ValueError):
            return _err("bad order_id", 400)

        from database import db
        from services import smm_panel as smm_svc

        order = await db.promo_get_order(pool, order_id)
        if not order or order["owner_id"] != uid:
            return _err("Заказ не найден", 404)
        if not order["bot_id"] or not order["smm_panel_id"]:
            return _err("У заказа не выбраны бот и SMM-панель", 400)

        panel = await db.smm_get_panel(pool, order["smm_panel_id"])
        if not panel:
            return _err("SMM-панель не найдена", 404)
        bot_rec = await db.warehouse_get_bot(pool, order["bot_id"])
        if not bot_rec:
            return _err("Бот не найден в складе", 404)

        link = f"https://t.me/{bot_rec['bot_username']}"
        client = smm_svc.make_client(panel["api_url"], panel["api_key_enc"])
        result = await client.add_order(
            service_id=panel["service_id"] or "",
            link=link,
            quantity=int(order["target_subs"] or 100),
        )
        if result.get("error") or "order" not in result:
            err_msg = result.get("error", str(result)[:200])
            await db.promo_log(
                pool, uid, "booster",
                f"Ошибка запуска накрутки заказ #{order['id']}: {err_msg}",
                level="ERROR", order_id=order["id"],
                meta={"panel": panel["name"], "link": link},
            )
            return _err(f"Ошибка панели: {str(err_msg)[:300]}", 400)

        smm_order_id = str(result["order"])
        await db.promo_update_order_status(
            pool, order["id"], "boosting",
            smm_order_id=smm_order_id,
            smm_panel_id=panel["id"],
        )
        await db.warehouse_update_bot(pool, bot_rec["id"], uid, status="working")
        await db.promo_log(
            pool, uid, "booster",
            f"Накрутка запущена: заказ #{order['id']} → панель #{smm_order_id}",
            order_id=order["id"],
            meta={"panel": panel["name"], "smm_order_id": smm_order_id, "link": link},
        )
        return _json_resp({"ok": True, "smm_order_id": smm_order_id})

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

    async def add_funnel_step(request: web.Request) -> web.Response:
        """Добавить шаг в воронку (multi-step drip). funnel_steps умел delay_minutes и
        порядок, но эндпоинта добавления шага не было — воронка застревала на шаге 1."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            funnel_id = int(request.match_info["funnel_id"])
            body = await request.json()
        except Exception:
            return _err("Invalid request", 400)
        # Владение через managed_bots.
        funnel = await _safe_fetchrow(pool,
            """SELECT f.id FROM funnels f JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE f.id=$1 AND mb.added_by=$2""", funnel_id, uid)
        if not funnel:
            return _err("Воронка не найдена", 404)
        message_text = (body.get("message_text") or "").strip()
        if not message_text:
            return _err("Текст шага обязателен", 400)
        if len(message_text) > 4096:
            return _err("Текст шага — до 4096 символов", 400)
        try:
            delay_minutes = max(0, min(43200, int(body.get("delay_minutes") or 0)))  # ≤30 дней
        except (TypeError, ValueError):
            return _err("delay_minutes должен быть числом", 400)
        # Ограничение на число шагов (защита от разрастания).
        cnt = await _safe_count(pool, "SELECT COUNT(*) FROM funnel_steps WHERE funnel_id=$1", funnel_id)
        if cnt >= 50:
            return _err("Достигнут лимит шагов (50)", 400)
        try:
            row = await pool.fetchrow(
                """INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes)
                   VALUES($1, COALESCE((SELECT MAX(step_order) FROM funnel_steps WHERE funnel_id=$1),0)+1, $2, $3)
                   RETURNING id, step_order""",
                funnel_id, message_text, delay_minutes)
            return _json_resp({"ok": True, "step_id": row["id"], "step_order": row["step_order"]})
        except Exception:
            log.exception("add_funnel_step funnel=%d uid=%d", funnel_id, uid)
            return _err("Не удалось добавить шаг", 500)

    async def delete_funnel_step(request: web.Request) -> web.Response:
        """Удалить шаг воронки. Шаг 1 (первое сообщение) удалить нельзя — это триггерный старт."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            step_id = int(request.match_info["step_id"])
        except (KeyError, ValueError):
            return _err("Invalid step_id", 400)
        # Владение + защита шага 1 через JOIN.
        step = await _safe_fetchrow(pool,
            """SELECT fs.id, fs.step_order FROM funnel_steps fs
               JOIN funnels f ON f.id=fs.funnel_id
               JOIN managed_bots mb ON mb.bot_id=f.bot_id
               WHERE fs.id=$1 AND mb.added_by=$2""", step_id, uid)
        if not step:
            return _err("Шаг не найден", 404)
        if int(step["step_order"]) <= 1:
            return _err("Первый шаг удалить нельзя (это стартовое сообщение)", 400)
        try:
            await pool.execute("DELETE FROM funnel_steps WHERE id=$1", step_id)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("delete_funnel_step step=%d uid=%d", step_id, uid)
            return _err("Не удалось удалить шаг", 500)

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
        repeat_min = schedule_repeat_minutes(body.get("repeat"))
        try:
            try:
                row = await pool.fetchrow(
                    "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by, repeat_interval_min) "
                    "VALUES($1,$2,$3,$4,$5) RETURNING id",
                    bot_id, text, dt, uid, repeat_min)
            except asyncpg.UndefinedColumnError:
                # schema_v145 ещё не применена — падаем на одноразовое.
                row = await pool.fetchrow(
                    "INSERT INTO scheduled_broadcasts(bot_id, message_text, execute_at, created_by) VALUES($1,$2,$3,$4) RETURNING id",
                    bot_id, text, dt, uid)
            return _json_resp({"ok": True, "id": row["id"], "repeat_min": repeat_min})
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
        text = validate_string(body.get("text"), max_len=4096)
        delay = validate_integer(body.get("delay", 30), min_val=-1, max_val=300) or 30
        channel_ids = body.get("channel_ids")  # optional list; None = all channels
        if not text:
            return _err("text required")
        if check_sql_suspicious(text):
            return _err("Invalid characters in text")
        if delay not in (5, 30, 60, -1):
            delay = 30
        # Validate channel IDs if provided
        if channel_ids:
            try:
                channel_ids = [int(x) for x in channel_ids if str(x).lstrip("-").isdigit()]
            except (TypeError, ValueError):
                return _err("Invalid channel IDs", 400)
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
        # is_backup может ещё не примениться (лаг миграции) — тогда селект с колонкой
        # упадёт; фолбэк без неё, чтобы список прокси НИКОГДА не ломался.
        try:
            rows = await pool.fetch(
                """SELECT id, label, proxy_url, proxy_type, is_active, is_alive, last_check,
                          created_at, COALESCE(is_backup, FALSE) AS is_backup
                   FROM user_proxies WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 200""", uid)
        except Exception:
            rows = await _safe_fetch(pool,
                """SELECT id, label, proxy_url, proxy_type, is_active, is_alive, last_check, created_at
                   FROM user_proxies WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 200""", uid)
            rows = [dict(r, is_backup=False) for r in rows]
        # proxy_url хранится зашифрованным — расшифровываем для отображения (passthrough legacy)
        from services.token_vault import decrypt_token

        out = []
        for r in rows:
            d = dict(r)
            if d.get("proxy_url"):
                d["proxy_url"] = decrypt_token(d["proxy_url"])
            out.append(d)
        return _json_resp({"proxies": out})

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
        proxy_type = parse_proxy_type(proxy_url)
        if not proxy_type:
            return _err("proxy_url must start with socks5://, socks4://, or http://")
        try:
            # шифруем at-rest; дедуп по детерминированному proxy_fp (шифр недетерминирован)
            from services.token_vault import encrypt_token, proxy_fingerprint

            _fp = proxy_fingerprint(proxy_url)
            _enc = encrypt_token(proxy_url)
            try:
                row = await pool.fetchrow(
                    """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type, proxy_fp)
                       VALUES($1,$2,$3,$4,$5)
                       ON CONFLICT(owner_id, proxy_fp) WHERE proxy_fp IS NOT NULL DO UPDATE
                       SET label=EXCLUDED.label RETURNING id""",
                    uid, label, _enc, proxy_type, _fp)
            except asyncpg.UndefinedColumnError:
                # proxy_fp ещё не мигрирован (лаг деплоя schema_v146) — фолбэк без него,
                # чтобы «добавить прокси» работало ВСЕГДА (без прокси не работает ничего).
                # Шифротекст уникален → дублей на UNIQUE(owner_id, proxy_url) не будет.
                row = await pool.fetchrow(
                    """INSERT INTO user_proxies(owner_id, label, proxy_url, proxy_type)
                       VALUES($1,$2,$3,$4) RETURNING id""",
                    uid, label, _enc, proxy_type)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception as e:
            log.exception("add_proxy uid=%d", uid)
            return _err(f"Не удалось добавить прокси: {str(e)[:120]}", 400)

    async def delete_proxy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Invalid proxy_id", 400)
        try:
            from services import proxy_hygiene
            # Гард изоляции: FK tg_accounts.proxy_id = ON DELETE SET NULL. Удалить
            # назначенный прокси = молча обнулить proxy_id аккаунтов → они уйдут
            # напрямую с домашнего IP → AUTH_KEY_DUPLICATED. Не даём — сначала detach.
            assigned = await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND proxy_id=$2",
                uid, proxy_id) or 0
            if not proxy_hygiene.can_delete_safely(assigned):
                return _err(
                    f"Прокси назначен {assigned} аккаунт(ам). Сначала снимите назначение "
                    f"(«Снять прокси»), иначе аккаунты уйдут напрямую и рискуют "
                    f"AUTH_KEY_DUPLICATED.", 409)
            await pool.execute(
                "DELETE FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
            return _json_resp({"ok": True})
        except Exception:
            return _err("Failed to delete proxy", 500)

    async def proxy_cleanup_dead(request: web.Request) -> web.Response:
        """Массово удалить подтверждённо-мёртвые (is_alive IS FALSE) НЕназначенные
        прокси. Назначенные и непроверенные не трогаем (изоляция). Read→delete."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Кандидаты: мёртвые пробой И не назначенные ни одному аккаунту.
            removed = await pool.fetch(
                """DELETE FROM user_proxies up
                   WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                     AND NOT EXISTS (
                         SELECT 1 FROM tg_accounts a
                         WHERE a.owner_id=$1 AND a.proxy_id=up.id)
                   RETURNING id""", uid)
            # Пропущенные мёртвые (назначены) — для честного отчёта оператору.
            skipped = await _safe_fetchval(pool,
                """SELECT COUNT(*) FROM user_proxies up
                   WHERE up.owner_id=$1 AND up.is_alive IS FALSE
                     AND EXISTS (SELECT 1 FROM tg_accounts a
                                 WHERE a.owner_id=$1 AND a.proxy_id=up.id)""", uid) or 0
            return _json_resp({"ok": True, "removed": len(removed),
                               "skipped_assigned": int(skipped)})
        except Exception as e:
            log.exception("proxy_cleanup_dead uid=%d", uid)
            return _err(str(e)[:120], 500)

    async def proxy_export(request: web.Request) -> web.Response:
        """Выгрузить список прокси (CSV/JSON) для аудита. Креды замаскированы —
        не выгружаем логин/пароль в файл. Показывает назначение и здоровье."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        fmt = request.query.get("format", "csv")
        try:
            from services import proxy_hygiene
            from services.token_vault import decrypt_token
            rows = await pool.fetch(
                """SELECT up.id, up.label, up.proxy_url, up.proxy_type, up.geo_country,
                          up.is_active, up.is_alive, up.last_check,
                          COALESCE(up.is_backup, FALSE) AS is_backup,
                          (SELECT COUNT(*) FROM tg_accounts a
                           WHERE a.owner_id=$1 AND a.proxy_id=up.id) AS assigned
                   FROM user_proxies up WHERE up.owner_id=$1 ORDER BY up.id""", uid)
            def _masked(enc):
                try:
                    return proxy_hygiene.mask_proxy_url(decrypt_token(enc))
                except Exception:
                    return proxy_hygiene.mask_proxy_url(enc)
            items = []
            for r in rows:
                items.append({
                    "id": r["id"], "label": r["label"] or "",
                    "url": _masked(r["proxy_url"]), "type": r["proxy_type"] or "",
                    "geo": r["geo_country"] or "", "active": bool(r["is_active"]),
                    "alive": (None if r["is_alive"] is None else bool(r["is_alive"])),
                    "last_check": str(r["last_check"] or ""), "backup": bool(r["is_backup"]),
                    "assigned": int(r["assigned"] or 0),
                })
            if fmt == "json":
                return _json_resp({"proxies": items})
            header = ["ID", "Метка", "URL (маска)", "Тип", "Гео", "Активен",
                      "Живой", "Проверен", "Резерв", "Назначен аккаунтам"]
            data = [[
                it["id"], it["label"], it["url"], it["type"], it["geo"],
                "да" if it["active"] else "нет",
                ("?" if it["alive"] is None else ("да" if it["alive"] else "нет")),
                it["last_check"], "да" if it["backup"] else "нет", it["assigned"],
            ] for it in items]
            return _csv_resp("proxies.csv", header, data)
        except Exception as e:
            log.exception("proxy_export uid=%d", uid)
            return _err(str(e)[:120], 500)

    async def check_proxy(request: web.Request) -> web.Response:
        """Проверить живость прокси (probe → api.telegram.org), сохранить is_alive/last_check.
        Возможность test_proxy/check_proxy_health раньше была недоступна в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Invalid proxy_id", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, proxy_url FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, uid)
        if not row:
            return _err("Прокси не найден", 404)
        from services.proxy_selector import probe_proxy
        res = await probe_proxy(row["proxy_url"])
        try:
            await pool.execute(
                "UPDATE user_proxies SET is_alive=$1, last_check=now() WHERE id=$2 AND owner_id=$3",
                bool(res.get("ok")), proxy_id, uid)
        except Exception:
            log_exc_swallow(log, "check_proxy persist")
        return _json_resp({"ok": True, "alive": bool(res.get("ok")),
                           "latency_ms": res.get("latency_ms"), "error": res.get("error")})

    async def proxy_toggle_backup(request: web.Request) -> web.Response:
        """Пометить/снять прокси как резервный (is_backup) для failover."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            proxy_id = int(request.match_info["proxy_id"])
        except (KeyError, ValueError):
            return _err("Invalid proxy_id", 400)
        row = await _safe_fetchrow(pool,
            "SELECT COALESCE(is_backup, FALSE) AS is_backup FROM user_proxies "
            "WHERE id=$1 AND owner_id=$2", proxy_id, uid)
        if row is None:
            return _err("Прокси не найден", 404)
        new_val = not bool(row["is_backup"])
        try:
            await pool.execute(
                "UPDATE user_proxies SET is_backup=$1 WHERE id=$2 AND owner_id=$3",
                new_val, proxy_id, uid)
        except Exception as e:
            log.exception("proxy_toggle_backup uid=%s proxy=%s", uid, proxy_id)
            return _err(f"Не удалось изменить: {str(e)[:120]}", 400)
        return _json_resp({"ok": True, "is_backup": new_val})

    async def proxy_failover(request: web.Request) -> web.Response:
        """Failover: переназначить аккаунты с мёртвым прокси на живой резервный."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.proxy_selector import failover_dead_proxies
            res = await asyncio.wait_for(failover_dead_proxies(pool, uid), timeout=180)
            return _json_resp({"ok": True, **res})
        except asyncio.TimeoutError:
            return _err("Проверка прокси заняла слишком долго — повторите", 400)
        except Exception as e:
            log.exception("proxy_failover uid=%s", uid)
            return _err(str(e)[:160], 500)

    async def proxies_isolation_check(request: web.Request) -> web.Response:
        """Проверка уникальности IP: активные аккаунты, делящие один IP прокси
        (нарушение изоляции → риск бана), + аккаунты без прокси."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services.proxy_selector import audit_proxy_isolation
            return _json_resp(await audit_proxy_isolation(pool, uid))
        except Exception as e:
            log.exception("proxies_isolation_check uid=%s", uid)
            return _err(str(e), 500)

    async def check_all_proxies(request: web.Request) -> web.Response:
        """Проверить все прокси владельца (ограниченная конкурентность)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rows = await _safe_fetch(pool,
            "SELECT id, proxy_url FROM user_proxies WHERE owner_id=$1 LIMIT 200", uid)
        if not rows:
            return _json_resp({"ok": True, "checked": 0, "alive": 0})
        from services.proxy_selector import probe_proxy
        sem = asyncio.Semaphore(10)
        async def _one(r):
            async with sem:
                res = await probe_proxy(r["proxy_url"])
                alive = bool(res.get("ok"))
                try:
                    await pool.execute(
                        "UPDATE user_proxies SET is_alive=$1, last_check=now() WHERE id=$2 AND owner_id=$3",
                        alive, r["id"], uid)
                except Exception:
                    log_exc_swallow(log, "check_all_proxies persist")
                return alive
        results = await asyncio.gather(*[_one(r) for r in rows], return_exceptions=True)
        alive = sum(1 for x in results if x is True)
        return _json_resp({"ok": True, "checked": len(rows), "alive": alive})

    async def import_proxies(request: web.Request) -> web.Response:
        """Массовый импорт прокси: вставленный список (по одному на строку)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        import re as _re
        raw = validate_string(body.get("proxies"), max_len=50000) or ""
        added, skipped = 0, 0
        for line in _re.split(r"[\n,;]+", raw):
            purl = line.strip()
            if not purl:
                continue
            # Validate proxy URL format
            if len(purl) > 500:
                skipped += 1
                continue
            # Block internal/loopback addresses
            if any(h in purl.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1")):
                skipped += 1
                continue
            ptype = parse_proxy_type(purl)
            if not ptype:
                skipped += 1
                continue
            try:
                await pool.execute(
                    """INSERT INTO user_proxies(owner_id, proxy_url, proxy_type)
                       VALUES($1,$2,$3) ON CONFLICT(owner_id, proxy_url) DO NOTHING""",
                    uid, purl, ptype)
                added += 1
            except Exception:
                skipped += 1
            if added >= 500:
                break
        return _json_resp({"ok": True, "added": added, "skipped": skipped})

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
        except Exception as e:
            log.warning("subscription fetch subscriptions: %s", e)
        # Фолбэк на platform_users только если источник истины недоступен.
        if not plan:
            try:
                prow = await pool.fetchrow(
                    "SELECT current_plan, plan_expires_at FROM platform_users WHERE user_id=$1", uid)
                if prow:
                    plan = prow["current_plan"] or "free"
                    if not expires and prow["plan_expires_at"]:
                        expires = str(prow["plan_expires_at"])
            except Exception as e:
                log.warning("subscription fetch platform_users: %s", e)
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
        except Exception as e:
            log.warning("user_settings_get: %s", e)
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
            # Немедленно применяем выбор политики прокси (не ждём старта операции).
            try:
                from services import account_manager as _am
                _am.set_owner_proxy_policy(uid, data.get("proxy_policy"))
            except Exception:
                pass
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
        except Exception as e:
            log.warning("payments_history uid=%d: %s", uid, e)
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

    def _presence_pack_dict(pack) -> dict:
        d = dict(pack)
        d["channel_ids"] = _jlist(pack.get("channel_ids"))
        d["group_ids"] = _jlist(pack.get("group_ids"))
        return d

    async def presence_pack_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("bad pack_id", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Pack not found", 404)
            return _json_resp(_presence_pack_dict(pack))
        except Exception as exc:
            log.exception("presence_pack_detail uid=%d pack_id=%d", uid, pack_id)
            return _err(str(exc), 500)

    async def presence_pack_config(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("bad pack_id", 400)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Pack not found", 404)

            if "bot_id" in body:
                bot_id = body.get("bot_id") or None
                bot_username = None
                if bot_id:
                    try:
                        bot_id = int(bot_id)
                    except (TypeError, ValueError):
                        return _err("Invalid bot_id", 400)
                    # Owner-scope: привязывать к паку можно ТОЛЬКО свой бот
                    # (added_by=uid), иначе — линковка чужого bot_id в свой пак.
                    bot_row = await pool.fetchrow(
                        "SELECT username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                        bot_id, uid,
                    )
                    if not bot_row:
                        return _err("Бот не найден или не принадлежит вам", 404)
                    bot_username = bot_row["username"]
                await pool.execute(
                    "UPDATE presence_packs SET bot_id=$3, bot_username=$4 WHERE id=$1 AND owner_id=$2",
                    pack_id, uid, bot_id, bot_username,
                )

            if "channel_ids" in body or "group_ids" in body:
                cur = _jlist(pack.get("channel_ids"))
                grp = _jlist(pack.get("group_ids"))
                channel_ids = body.get("channel_ids", cur)
                group_ids = body.get("group_ids", grp)
                if not isinstance(channel_ids, list) or not isinstance(group_ids, list):
                    return _err("channel_ids/group_ids must be lists", 400)
                await db.update_presence_pack_channels(
                    pool, pack_id, uid,
                    [int(x) for x in channel_ids],
                    [int(x) for x in group_ids],
                )

            updated = await db.get_presence_pack(pool, pack_id, uid)
            return _json_resp(_presence_pack_dict(updated))
        except Exception as exc:
            log.exception("presence_pack_config uid=%d pack_id=%d", uid, pack_id)
            return _err(str(exc), 500)

    async def presence_pack_seed(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("bad pack_id", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Pack not found", 404)
            ch_ids = _jlist(pack.get("channel_ids"))
            if not ch_ids:
                return _err("Нет каналов в пакете", 400)
            from services.operation_bus import submit
            op_id = await submit(
                pool, uid, "seed_presence_pack", {"pack_id": pack_id},
                total_items=len(ch_ids),
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("presence_pack_seed uid=%d pack_id=%d", uid, pack_id)
            return _err(str(exc), 500)

    async def presence_pack_promote(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pack_id = int(request.match_info["pack_id"])
        except (KeyError, ValueError):
            return _err("bad pack_id", 400)
        try:
            from database import db
            pack = await db.get_presence_pack(pool, pack_id, uid)
            if not pack:
                return _err("Pack not found", 404)
            if not pack.get("bot_id"):
                return _err("Нет бота в пакете. Привяжите бот перед назначением admin.", 400)
            ch_ids = _jlist(pack.get("channel_ids"))
            gr_ids = _jlist(pack.get("group_ids"))
            all_asset_ids = ch_ids + gr_ids
            if not all_asset_ids:
                return _err("Нет каналов/групп в пакете", 400)
            from services.operation_bus import submit
            op_id = await submit(
                pool, uid, "promote_presence_pack",
                {
                    "pack_id": pack_id,
                    "bot_tg_id": pack["bot_id"],
                    "channel_ids": all_asset_ids,
                },
                total_items=len(all_asset_ids),
            )
            return _json_resp({"ok": True, "op_id": op_id})
        except Exception as exc:
            log.exception("presence_pack_promote uid=%d pack_id=%d", uid, pack_id)
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

    async def global_presence_create(request: web.Request) -> web.Response:
        """Create a new Global Presence plan with channels/groups/bots."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        
        asset_type = body.get("asset_type", "channel")
        name_pattern = (body.get("name_pattern") or "").strip()
        username_pattern = (body.get("username_pattern") or "").strip()
        description = (body.get("description") or "").strip()
        short_desc = (body.get("short_description") or "").strip()
        account_ids = body.get("account_ids") or []
        ecosystem_id = body.get("ecosystem_id")

        # ── Референс-унификация: из образца названия/username + города-образца
        # автоматически выводим паттерн ('Новости Москва' + 'Москва' → 'Новости
        # {{CITY_NAME}}'). Если явный паттерн не задан, но задан референс — деривуем.
        from services.presence_planner import (
            build_targets, derive_pattern_from_reference, render_pattern,
        )
        ref_name = (body.get("reference_name") or "").strip()
        ref_username = (body.get("reference_username") or "").strip()
        ref_city = (body.get("reference_city") or "").strip()
        ref_city_slug = (body.get("reference_city_slug") or "").strip()
        if not name_pattern and ref_name:
            name_pattern = derive_pattern_from_reference(ref_name, ref_city, "{{CITY_NAME}}")
        if not username_pattern and ref_username:
            username_pattern = derive_pattern_from_reference(
                ref_username, ref_city_slug or ref_city, "{{CITY_SLUG}}")

        if asset_type not in ("channel", "group", "bot", "package", "full_package"):
            return _err("Invalid asset_type: must be channel/group/bot/package/full_package", 400)
        if not name_pattern:
            return _err("Укажите паттерн названия или референс", 400)

        # ── Гео: пресет (страны/города мира) ИЛИ свой список городов ИЛИ страны.
        from services.geo_data import (
            GEO_PRESETS, parse_custom_geo_list, enrich_geo_list, filter_preset_cities,
        )
        geo_preset = (body.get("geo_preset") or "").strip()
        custom_cities = (body.get("custom_cities") or "").strip()
        countries = body.get("countries") or []
        preset_cities = body.get("preset_cities") or []  # выбранное подмножество городов пресета
        geo_list: list = []
        geo_source = ""
        if geo_preset and geo_preset in GEO_PRESETS:
            # Пустой preset_cities → весь пресет; иначе — только отмеченные города.
            geo_list = filter_preset_cities(geo_preset, preset_cities)
            geo_source = geo_preset
        elif custom_cities:
            geo_list = parse_custom_geo_list(custom_cities)
            geo_source = "custom"
        elif countries:
            # Фолбэк на старый ввод: страны как «города» (минимальные geo-словари).
            geo_list = enrich_geo_list([
                {"country": str(c).strip(), "city": str(c).strip()} for c in countries if str(c).strip()
            ])
            geo_source = "countries"
        if not geo_list:
            return _err("Выберите гео-пресет, свои города или страны", 400)
        # Потолок против гигантских планов.
        geo_list = geo_list[:200]

        acc_ids_int = [int(x) for x in account_ids if str(x).isdigit()]
        if acc_ids_int:
            owned = await _safe_fetch(pool,
                "SELECT id FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
                uid, acc_ids_int)
            acc_ids_int = [int(r["id"]) for r in (owned or [])]

        # Генерируем цели тем же конвейером, что и бот (build_targets → рендер
        # плейсхолдеров per-city). Раньше mini-app путь цели НЕ создавал вообще —
        # план ставился в очередь, а исполнитель находил 0 целей.
        targets = build_targets(
            geo_list,
            "channel" if asset_type in ("package", "full_package") else asset_type,
            name_pattern, username_pattern or None, acc_ids_int,
        )
        if not targets:
            return _err("Не удалось сгенерировать цели из гео", 400)
        # Превью первых названий/username для ответа.
        preview = [
            {"name": t.get("planned_name"), "username": t.get("planned_username")}
            for t in targets[:5]
        ]

        try:
            # status='pending' — иначе launch (требует pending/failed) отклонял бы 'draft'.
            plan_id = await pool.fetchval(
                """INSERT INTO global_presence_plans
                   (owner_id, asset_type, name_pattern, username_pattern,
                    geo_selection, account_selection, status)
                   VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, 'pending')
                   RETURNING id""",
                uid, asset_type, name_pattern, username_pattern,
                json.dumps({"geo_source": geo_source, "count": len(targets),
                            "description": description, "short_description": short_desc}),
                json.dumps({"account_ids": acc_ids_int}),
            )
            from database.db import create_global_presence_targets
            await create_global_presence_targets(pool, plan_id, targets)
            # Привязка к экосистеме
            if ecosystem_id:
                try:
                    await pool.execute(
                        "INSERT INTO ecosystem_global_presence (ecosystem_id, plan_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        int(ecosystem_id), plan_id,
                    )
                except Exception as e:
                    log.warning("global_presence_create ecosystem link: %s", e)
            return _json_resp({
                "ok": True, "plan_id": plan_id, "asset_type": asset_type,
                "name_pattern": name_pattern, "username_pattern": username_pattern,
                "targets": len(targets), "geo_source": geo_source, "preview": preview,
            })
        except Exception as exc:
            log.exception("global_presence_create uid=%d", uid)
            return _err(str(exc), 500)

    async def geo_presets(request: web.Request) -> web.Response:
        """Список гео-пресетов (страны/города мира) для Global Presence.
        ?cities=<key> — вернуть города конкретного пресета для выбора галочками."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services.geo_data import GEO_PRESETS, preset_city_options
        want = (request.rel_url.query.get("cities") or "").strip()
        if want:
            if want not in GEO_PRESETS:
                return _err("Неизвестный пресет", 404)
            return _json_resp({"key": want, "cities": preset_city_options(want)})
        return _json_resp({"presets": [
            {"key": k, "label": v.get("label", k), "count": v.get("count", len(v.get("cities", [])))}
            for k, v in GEO_PRESETS.items()
        ]})

    async def global_presence_launch(request: web.Request) -> web.Response:
        """Launch a Global Presence plan — creates targets and queues operations."""
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
                return _err("Plan not found", 404)
            if plan["status"] not in ("pending", "failed"):
                return _err("Plan already running or completed", 400)
            
            # Queue the operation
            from services import operation_bus
            op_type = f"global_presence_{plan['asset_type']}"
            op_id = await operation_bus.submit(
                pool, uid, op_type,
                {"plan_id": plan_id, "asset_type": plan["asset_type"]},
                total_items=0,
            )
            return _json_resp({"ok": True, "op_id": op_id, "plan_id": plan_id})
        except Exception as exc:
            log.exception("global_presence_launch uid=%d plan=%d", uid, plan_id)
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

    async def ecosystem_auto_discover(request: web.Request) -> web.Response:
        """Авто-наполнение экосистемы объектами (аккаунты/каналы/боты по region/пулам).
        Возможность ecosystem_brain.auto_discover_members раньше не была выведена в UI."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            eco_id = int(request.match_info["eco_id"])
        except (KeyError, ValueError):
            return _err("bad eco_id", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM ecosystems WHERE id=$1 AND owner_id=$2", eco_id, uid)
        if not owns:
            return _err("Экосистема не найдена", 404)
        try:
            from services import ecosystem_brain
            added = await ecosystem_brain.auto_discover_members(pool, eco_id, uid)
            total = sum(int(v) for v in (added or {}).values())
            return _json_resp({"ok": True, "added": added or {}, "total": total})
        except Exception as exc:
            log.exception("ecosystem_auto_discover uid=%d eco=%d", uid, eco_id)
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

    async def content_mesh_targets_list(request: web.Request) -> web.Response:
        """Список целевых каналов меша. Без целей меш ничего не репостит (runner
        читает mesh_targets) — в mini-app их раньше нельзя было задать вообще."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
        except (KeyError, ValueError):
            return _err("bad mesh_id", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid)
        if not owns:
            return _err("Меш не найден", 404)
        rows = await _safe_fetch(pool,
            "SELECT id, target_channel, enabled, added_at FROM mesh_targets WHERE mesh_id=$1 ORDER BY id", mesh_id)
        return _json_resp({"targets": [dict(r) for r in rows]})

    async def content_mesh_target_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            mesh_id = int(request.match_info["mesh_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM content_meshes WHERE id=$1 AND owner_id=$2", mesh_id, uid)
        if not owns:
            return _err("Меш не найден", 404)
        target = (body.get("target_channel") or "").strip()
        if not target:
            return _err("Укажите канал-цель (@username или -100…)", 400)
        if len(target) > 128:
            return _err("Слишком длинный идентификатор", 400)
        cnt = await _safe_count(pool, "SELECT COUNT(*) FROM mesh_targets WHERE mesh_id=$1", mesh_id)
        if cnt >= 200:
            return _err("Достигнут лимит целей (200)", 400)
        try:
            # Идемпотентно: повтор включает существующую цель, а не плодит дубли.
            existing = await pool.fetchrow(
                "SELECT id FROM mesh_targets WHERE mesh_id=$1 AND target_channel=$2", mesh_id, target)
            if existing:
                await pool.execute("UPDATE mesh_targets SET enabled=TRUE WHERE id=$1", existing["id"])
                return _json_resp({"ok": True, "id": existing["id"], "reactivated": True})
            row = await pool.fetchrow(
                "INSERT INTO mesh_targets(mesh_id, target_channel) VALUES($1,$2) RETURNING id",
                mesh_id, target)
            return _json_resp({"ok": True, "id": row["id"]})
        except Exception:
            log.exception("content_mesh_target_add mesh=%d uid=%d", mesh_id, uid)
            return _err("Не удалось добавить цель", 500)

    async def content_mesh_target_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            target_id = int(request.match_info["target_id"])
        except (KeyError, ValueError):
            return _err("bad target_id", 400)
        # Владение через JOIN на content_meshes.
        row = await _safe_fetchrow(pool,
            """SELECT mt.id FROM mesh_targets mt
               JOIN content_meshes cm ON cm.id=mt.mesh_id
               WHERE mt.id=$1 AND cm.owner_id=$2""", target_id, uid)
        if not row:
            return _err("Цель не найдена", 404)
        try:
            await pool.execute("DELETE FROM mesh_targets WHERE id=$1", target_id)
            return _json_resp({"ok": True})
        except Exception:
            log.exception("content_mesh_target_delete target=%d uid=%d", target_id, uid)
            return _err("Не удалось удалить цель", 500)

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
        """Создаёт и сразу запускает кампанию (как мастер в боте) — а не пустой
        черновик: без выбранных каналов посты некому публиковать, и раньше
        кампания, созданная тут, навсегда оставалась 'draft' с 0 постов."""
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
        channel_ids = data.get("channel_ids") or []
        if not topic or not core_message:
            return _err("topic и core_message обязательны", 400)
        if not channel_ids:
            return _err("Выберите хотя бы один канал", 400)
        if campaign_type not in ("trend", "launch", "awareness", "counter"):
            campaign_type = "trend"
        if spread_hours < 1:
            spread_hours = 1
        try:
            req_ids = [int(x) for x in channel_ids if str(x).lstrip("-").isdigit()]
        except (TypeError, ValueError):
            req_ids = []
        rows = await _safe_fetch(pool,
            "SELECT channel_id, acc_id, username, title FROM managed_channels "
            "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])", uid, req_ids)
        if not rows:
            return _err("Каналы не найдены", 404)

        from services.ai_providers import configured_providers
        from services import narrative_engine
        providers = configured_providers()
        ai_provider = providers[0] if providers else None
        if ai_provider is None:
            return _err("AI-провайдер не настроен — добавьте API-ключ в Настройках", 400)

        channel_usernames = [r["username"] or str(r["channel_id"]) for r in rows]
        channel_meta = [{"channel_id": r["channel_id"], "acc_id": r["acc_id"]} for r in rows]
        try:
            cid = await narrative_engine.create_campaign(
                pool=pool,
                owner_id=uid,
                topic=topic,
                core_message=core_message,
                channel_usernames=channel_usernames,
                channel_meta=channel_meta,
                spread_hours=spread_hours,
                campaign_type=campaign_type,
                ai_provider=ai_provider,
            )
            return _json_resp({"id": cid, "ok": True})
        except Exception as exc:
            log.exception("narrative_campaign_create uid=%d", uid)
            return _err(str(exc), 500)

    # ── Spintax ──────────────────────────────────────────────────────────────

    async def spintax_generate(request: web.Request) -> web.Response:
        """Обычный текст → spintax-шаблоны (через LLM) + пример раскрытия."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        script = str(data.get("script", "")).strip()
        if not script:
            return _err("Пришлите текст сценария", 400)
        if len(script) > 4000:
            return _err("Слишком длинный текст (лимит 4000 символов)", 400)

        from services import spintax_ai, spintax_service

        try:
            templates = await spintax_service.generate_spins(
                script,
                complete=spintax_ai.complete,
                count=spintax_service.DEFAULT_SPIN_COUNT,
            )
        except spintax_service.SpintaxServiceError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("spintax_generate uid=%d", uid)
            return _err("Внутренняя ошибка генерации", 500)
        return _json_resp({"variants": _spintax_pack(templates)})

    async def spintax_expand(request: web.Request) -> web.Response:
        """Быстрая перегенерация: раскрыть готовые шаблоны заново, без LLM."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("bad json", 400)
        templates = data.get("templates") or []
        single = str(data.get("template", "")).strip()
        if single:
            templates = [single]
        templates = [str(t).strip() for t in templates if str(t).strip()]
        if not templates:
            return _err("Нет шаблонов для раскрытия", 400)

        from services import spintax_service

        for tpl in templates:
            if not spintax_service.is_valid_template(tpl):
                return _err("Некорректный spintax-шаблон", 400)
        # expand = «Ещё генерация» → показываем случайную комбинацию
        return _json_resp({"variants": _spintax_pack(templates, random_sample=True)})

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

    async def audience_dna_profile(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Not found", 404)

            dna = await dna_svc.get_dna(pool, bot_id)
            if not dna:
                return _json_resp({"computed": False})

            data = _dna_to_dict(dna)
            data["computed"] = True
            data["recommendations"] = dna_svc.generate_recommendations(dna)
            return _json_resp(data)
        except Exception as exc:
            log.exception("audience_dna_profile uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    async def audience_dna_compute(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Not found", 404)

            dna = await dna_svc.compute_dna(pool, bot_id, uid)
            data = _dna_to_dict(dna)
            data["computed"] = True
            data["recommendations"] = dna_svc.generate_recommendations(dna)
            return _json_resp(data)
        except Exception as exc:
            log.exception("audience_dna_compute uid=%d bot=%d", uid, bot_id)
            return _err(str(exc), 500)

    async def audience_dna_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
        except (KeyError, ValueError):
            return _err("bad bot_id", 400)
        try:
            from database import db as _db
            from services import audience_dna as dna_svc

            bot_row = await _db.get_bot(pool, bot_id, uid)
            if not bot_row:
                return _err("Not found", 404)

            history = await dna_svc.get_dna_history(pool, bot_id, limit=10)
            return _json_resp([_dna_to_dict(snap) for snap in history])
        except Exception as exc:
            log.exception("audience_dna_history uid=%d bot=%d", uid, bot_id)
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

    async def topology_nodes(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            # tg_channels не имеет колонки is_active (есть только id/owner_id/title/
            # username/...) — раньше SELECT is_active валил эндпоинт 500. Каналы
            # считаем активными по факту наличия.
            channels = await pool.fetch(
                "SELECT id, username, title FROM tg_channels WHERE owner_id=$1 LIMIT 50", uid
            )
            # managed_bots скоупится по added_by, НЕ owner_id (такой колонки нет) —
            # прежний owner_id=$1 валил эндпоинт 500.
            bots = await pool.fetch(
                "SELECT bot_id, username, first_name, is_active FROM managed_bots WHERE added_by=$1 LIMIT 50", uid
            )
            nodes = []
            for ch in channels:
                nodes.append({"id": f"ch_{ch['id']}", "type": "channel", "name": ch['username'] or ch['title'] or f"#{ch['id']}", "active": True})
            for b in bots:
                nodes.append({"id": f"bot_{b['bot_id']}", "type": "bot", "name": f"@{b['username']}" if b['username'] else b['first_name'] or f"#{b['bot_id']}", "active": b['is_active']})
            return _json_resp({"nodes": nodes})
        except Exception as e:
            return _err(str(e), 500)

    async def topology_links(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            links = await pool.fetch(
                """SELECT a.channel_id as ch1, b.channel_id as ch2,
                          COUNT(DISTINCT a.user_id) as strength
                   FROM channel_members a
                   JOIN channel_members b ON a.user_id = b.user_id AND a.channel_id < b.channel_id
                   WHERE a.channel_id IN (SELECT id FROM tg_channels WHERE owner_id=$1)
                   GROUP BY a.channel_id, b.channel_id
                   HAVING COUNT(DISTINCT a.user_id) > 5
                   ORDER BY strength DESC LIMIT 30""",
                uid
            )
            return _json_resp({"links": [{"from": f"ch_{l['ch1']}", "to": f"ch_{l['ch2']}", "strength": l['strength']} for l in links]})
        except Exception as e:
            return _err(str(e), 500)

    async def schedule_post(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            channel_id = data.get("channel_id")
            text = data.get("text", "")
            scheduled_at = data.get("scheduled_at")
            if not channel_id or not text or not scheduled_at:
                return _err("Missing channel_id, text, or scheduled_at")
            await pool.execute(
                """INSERT INTO operation_queue (owner_id, op_type, label, status, params, scheduled_for)
                   VALUES ($1, 'mass_publish', $2, 'pending', $3, $4)""",
                uid, f"Scheduled post to #{channel_id}",
                json.dumps({"channel_ids": [channel_id], "text": text}),
                scheduled_at
            )
            return _json_resp({"status": "scheduled"})
        except Exception as e:
            return _err(str(e), 500)

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

        async def fetch_op_progress() -> list:
            """Fetch running operations with progress for real-time updates."""
            try:
                rows = await pool.fetch(
                    """SELECT id, op_type, COALESCE(label, op_type) AS label,
                              total_items, done_items, status, error_msg
                       FROM operation_queue WHERE owner_id=$1 AND status='running'
                       ORDER BY created_at DESC""",
                    uid)
                result = []
                for r in rows:
                    total = r["total_items"] or 0
                    done = r["done_items"] or 0
                    pct = int(done * 100 / total) if total > 0 else 0
                    result.append({
                        "id": r["id"],
                        "op_type": r["op_type"],
                        "label": r["label"],
                        "total": total,
                        "done": done,
                        "pct": pct,
                        "status": r["status"],
                    })
                return result
            except Exception:
                return []

        _seen_completed: set[int] = set()

        async def fetch_completed_ops() -> list:
            try:
                rows = await pool.fetch(
                    """SELECT id, op_type, COALESCE(label, op_type) AS label,
                              total_items, done_items, status, error_msg
                       FROM operation_queue WHERE owner_id=$1 AND status IN ('done','failed')
                         AND finished_at > now() - make_interval(minutes => 30)
                       ORDER BY finished_at DESC LIMIT 20""",
                    uid)
                return [dict(r) for r in rows]
            except Exception:
                return []

        try:
            data = await _stats(pool, uid)
            await push("stats", data)
            await push("activity", {"items": await fetch_activity()})
            await push("op_progress", {"items": await fetch_op_progress()})
            while True:
                await asyncio.sleep(15)
                data = await _stats(pool, uid)
                await push("stats", data)
                await push("activity", {"items": await fetch_activity()})
                await push("op_progress", {"items": await fetch_op_progress()})
                completed = await fetch_completed_ops()
                for op in completed:
                    op_id = op["id"]
                    if op_id not in _seen_completed:
                        _seen_completed.add(op_id)
                        await push("op_complete", {
                            "id": op_id,
                            "op_type": op["op_type"],
                            "label": op["label"],
                            "status": op["status"],
                            "total": op["total_items"],
                            "done": op["done_items"],
                            "error_msg": op.get("error_msg"),
                        })
                if len(_seen_completed) > 500:
                    _seen_completed.clear()
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
    app.router.add_post("/api/miniapp/bot_factory/create", bot_factory_create)
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
    app.router.add_post("/api/miniapp/broadcast/{bc_id}/resend", broadcast_resend)
    app.router.add_get("/api/miniapp/broadcasts", broadcasts_list)
    app.router.add_post("/api/miniapp/broadcast/schedule", broadcast_schedule)
    app.router.add_post("/api/miniapp/broadcast/ab_test", broadcast_ab_test)
    app.router.add_get("/api/miniapp/broadcast/{bc_id}/analytics", broadcast_analytics)
    # Channels
    app.router.add_get("/api/miniapp/channels", channels)
    # Campaigns / Funnels
    app.router.add_get("/api/miniapp/campaigns", campaigns)
    app.router.add_get("/api/miniapp/funnels", funnels_all)
    # Accounts
    app.router.add_get("/api/miniapp/accounts", accounts)
    app.router.add_get("/api/miniapp/accounts/export", accounts_export)
    app.router.add_get("/api/miniapp/account/{acc_id}", account_detail)
    # Channels
    app.router.add_get("/api/miniapp/channel/{ch_id}", channel_detail)
    app.router.add_post("/api/miniapp/channel/{ch_id}/post", post_to_channel)
    app.router.add_post("/api/miniapp/channel/{ch_id}/pin", pin_channel_last_post)
    app.router.add_get("/api/miniapp/channel/{ch_id}/invite_link", channel_invite_link)
    # Bot subscribers
    app.router.add_get("/api/miniapp/bot/{bot_id}/subscribers", bot_subscribers)
    # DM campaigns
    app.router.add_post("/api/miniapp/dm_campaign", create_dm_campaign)
    # Operations
    app.router.add_get("/api/miniapp/operations", operations)
    app.router.add_get("/api/miniapp/operation/{op_id}", operation_status)
    app.router.add_get("/api/miniapp/operation/{op_id}/log", operation_log)
    app.router.add_post("/api/miniapp/operation/{op_id}/cancel", cancel_operation)
    app.router.add_post("/api/miniapp/operation/{op_id}/retry", retry_operation)
    # Bot toggle
    app.router.add_put("/api/miniapp/bot/{bot_id}/toggle", toggle_bot)
    # Funnel steps
    app.router.add_get("/api/miniapp/funnel/{funnel_id}/steps", funnel_steps)
    app.router.add_post("/api/miniapp/funnel/{funnel_id}/step", add_funnel_step)
    app.router.add_delete("/api/miniapp/funnel/step/{step_id}", delete_funnel_step)
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
    app.router.add_post("/api/miniapp/schedule_post", schedule_post)
    # Proxies
    app.router.add_get("/api/miniapp/proxies", proxies)
    app.router.add_post("/api/miniapp/proxy", add_proxy)
    app.router.add_post("/api/miniapp/proxy/cleanup_dead", proxy_cleanup_dead)
    app.router.add_post("/api/miniapp/proxy/rotate", rotate_proxies)
    app.router.add_get("/api/miniapp/proxy/export", proxy_export)
    app.router.add_delete("/api/miniapp/proxy/{proxy_id}", delete_proxy)
    app.router.add_post("/api/miniapp/proxy/{proxy_id}/check", check_proxy)
    app.router.add_post("/api/miniapp/proxy/{proxy_id}/backup", proxy_toggle_backup)
    app.router.add_post("/api/miniapp/proxy/failover", proxy_failover)
    app.router.add_post("/api/miniapp/proxies/check_all", check_all_proxies)
    app.router.add_get("/api/miniapp/proxies/isolation_check", proxies_isolation_check)
    app.router.add_post("/api/miniapp/proxies/import", import_proxies)
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
    app.router.add_post("/api/miniapp/bot/{bot_id}/profile", bot_profile)
    app.router.add_post("/api/miniapp/bot/{bot_id}/avatar", bot_avatar)
    app.router.add_delete("/api/miniapp/bot/{bot_id}/avatar", bot_avatar)
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
    app.router.add_get("/api/miniapp/parser/audience/export", parsed_audience_export)
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
    app.router.add_post("/api/miniapp/promo/order/{order_id}/boost", promo_order_boost)
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
    app.router.add_post("/api/miniapp/warmup/bulk_start", warmup_bulk_start)
    app.router.add_delete("/api/miniapp/warmup/{plan_id}", warmup_delete_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/pause", warmup_pause_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/resume", warmup_resume_plan)
    app.router.add_post("/api/miniapp/warmup/{plan_id}/cancel", warmup_cancel_plan)
    # A/B Experiments
    app.router.add_get("/api/miniapp/experiments", experiments_list)
    app.router.add_post("/api/miniapp/experiment", experiment_create)
    app.router.add_get("/api/miniapp/experiment/{exp_id}", experiment_detail)
    app.router.add_delete("/api/miniapp/experiment/{exp_id}", experiment_delete)
    # Health Dashboard
    app.router.add_get("/api/miniapp/health", health_overview)
    # Topology Map
    app.router.add_get("/api/miniapp/topology", topology_overview)
    app.router.add_get("/api/miniapp/topology/nodes", topology_nodes)
    app.router.add_get("/api/miniapp/topology/links", topology_links)
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
    app.router.add_post("/api/miniapp/global_search", global_search)
    app.router.add_post("/api/miniapp/account/{acc_id}/login_code", account_login_code)
    app.router.add_post("/api/miniapp/account/{acc_id}/check_restriction", account_check_restriction)
    app.router.add_get("/api/miniapp/accounts/export_json", accounts_export_json)
    app.router.add_post("/api/miniapp/channel/add", channel_add)
    app.router.add_post("/api/miniapp/channel/{ch_id}/edit", channel_edit)
    app.router.add_post("/api/miniapp/channel/{ch_id}/promote", channel_promote)
    app.router.add_post("/api/miniapp/channels/mass", channels_mass)
    app.router.add_delete("/api/miniapp/channel/{ch_id}", channel_remove)
    app.router.add_post("/api/miniapp/account/{acc_id}/toggle", account_toggle)
    app.router.add_post("/api/miniapp/account/{acc_id}/check", account_check_one)
    app.router.add_post("/api/miniapp/account/{acc_id}/action/{act}", account_action)
    app.router.add_post("/api/miniapp/account/{acc_id}/spamblock_appeal", account_spamblock_appeal)
    app.router.add_post("/api/miniapp/account/{acc_id}/post_story", account_post_story)
    app.router.add_post("/api/miniapp/account/{acc_id}/proxy", account_set_proxy)
    app.router.add_post("/api/miniapp/account/{acc_id}/note", account_set_note)
    app.router.add_post("/api/miniapp/account/{acc_id}/meta", account_set_meta)
    app.router.add_delete("/api/miniapp/account/{acc_id}", account_delete)
    app.router.add_post("/api/miniapp/boost", boost_submit)
    app.router.add_post("/api/miniapp/growth", growth_submit)
    app.router.add_post("/api/miniapp/ai_comment", ai_comment_submit)
    app.router.add_post("/api/miniapp/compliance_scan", compliance_scan_submit)
    app.router.add_post("/api/miniapp/reporter", reporter_submit)
    # Quick Post
    app.router.add_post("/api/miniapp/quick_post", quick_post_submit)
    # SEO
    app.router.add_get("/api/miniapp/seo", seo_overview)
    app.router.add_post("/api/miniapp/seo/apply", seo_apply)
    app.router.add_post("/api/miniapp/seo/apply_all", seo_apply_all)
    app.router.add_post("/api/miniapp/seo/apply_bot", seo_apply_bot)
    app.router.add_post("/api/miniapp/ranking/reopt_setting", reopt_setting)
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
    app.router.add_get("/api/miniapp/presence_pack/{pack_id}", presence_pack_detail)
    app.router.add_put("/api/miniapp/presence_pack/{pack_id}/config", presence_pack_config)
    app.router.add_post("/api/miniapp/presence_pack/{pack_id}/seed", presence_pack_seed)
    app.router.add_post("/api/miniapp/presence_pack/{pack_id}/promote", presence_pack_promote)
    # Global Presence
    app.router.add_get("/api/miniapp/global_presence", global_presence_plans)
    app.router.add_get("/api/miniapp/global_presence/{plan_id}", global_presence_plan_detail)
    app.router.add_post("/api/miniapp/global_presence", global_presence_create)
    app.router.add_get("/api/miniapp/geo_presets", geo_presets)
    app.router.add_post("/api/miniapp/global_presence/{plan_id}/launch", global_presence_launch)
    # Mass Ops
    app.router.add_get("/api/miniapp/mass_ops", mass_ops_overview)
    # Ecosystems
    app.router.add_get("/api/miniapp/ecosystems", ecosystems_list)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}", ecosystem_detail)
    app.router.add_post("/api/miniapp/ecosystem/{eco_id}/auto_discover", ecosystem_auto_discover)
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
    app.router.add_get("/api/miniapp/content_mesh/{mesh_id}/targets", content_mesh_targets_list)
    app.router.add_post("/api/miniapp/content_mesh/{mesh_id}/target", content_mesh_target_add)
    app.router.add_delete("/api/miniapp/content_mesh/target/{target_id}", content_mesh_target_delete)
    app.router.add_put("/api/miniapp/content_mesh/{mesh_id}/toggle", content_mesh_toggle)
    # Narrative Engine
    app.router.add_get("/api/miniapp/narrative", narrative_campaigns_list)
    app.router.add_post("/api/miniapp/narrative", narrative_campaign_create)
    app.router.add_get("/api/miniapp/narrative/{campaign_id}", narrative_campaign_detail)
    # Spintax
    app.router.add_post("/api/miniapp/spintax/generate", spintax_generate)
    app.router.add_post("/api/miniapp/spintax/expand", spintax_expand)
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
    app.router.add_get("/api/miniapp/audience_dna/{bot_id}/profile", audience_dna_profile)
    app.router.add_post("/api/miniapp/audience_dna/{bot_id}/compute", audience_dna_compute)
    app.router.add_get("/api/miniapp/audience_dna/{bot_id}/history", audience_dna_history)
    # Auto Funnels
    app.router.add_get("/api/miniapp/auto_funnels", auto_funnels_list)
    app.router.add_post("/api/miniapp/auto_funnel", auto_funnel_create)
    app.router.add_put("/api/miniapp/auto_funnel/{funnel_id}/toggle", auto_funnel_toggle)
    app.router.add_get("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_detail)
    app.router.add_delete("/api/miniapp/auto_funnel/{funnel_id}", auto_funnel_delete)
    # ── Circuit Breaker & Adaptive Pacing ─────────────────────────────────────
    async def circuit_breaker_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import op_worker as _opw
            status = _opw._circuit_breaker_status(uid)
            return _json_resp(status)
        except Exception as e:
            return _err(str(e), 500)

    async def proxy_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import account_manager as _am, proxy_hygiene
            from services.token_vault import decrypt_token
            rows = await pool.fetch(
                """SELECT up.id, up.label, up.proxy_url, up.geo_country, up.is_active,
                          up.is_alive, up.last_check,
                          (SELECT COUNT(*) FROM tg_accounts a
                           WHERE a.owner_id=$1 AND a.proxy_id=up.id) AS assigned
                   FROM user_proxies up WHERE up.owner_id=$1 ORDER BY up.id""",
                uid,
            )
            stats = []
            for r in rows:
                # runtime-статы keyed по СТРОКЕ proxy_url из БД (тот же шифротекст
                # используется в test_proxy → ключи совпадают); в UI показываем
                # МАСКИРОВАННЫЙ расшифрованный url, а не сырой ENC:-шифротекст.
                s = _am.get_proxy_stats(r["proxy_url"])
                try:
                    disp = proxy_hygiene.mask_proxy_url(decrypt_token(r["proxy_url"]))
                except Exception:
                    disp = proxy_hygiene.mask_proxy_url(r["proxy_url"])
                stats.append({
                    "id": r["id"],
                    "label": r["label"] or "",
                    "url": disp,
                    "geo": r["geo_country"],
                    "active": bool(r["is_active"]),
                    "alive": (None if r["is_alive"] is None else bool(r["is_alive"])),
                    "last_check": str(r["last_check"] or ""),
                    "assigned": int(r["assigned"] or 0),
                    **s,
                })
            return _json_resp({"proxies": stats})
        except Exception as e:
            log.exception("proxy_stats uid=%s", uid)
            return _err(str(e), 500)

    async def ecosystem_recommendations(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import ecosystem_brain as _eb
            recs = await _eb.get_ecosystem_recommendations(pool, uid)
            return _json_resp({"recommendations": recs})
        except Exception as e:
            return _err(str(e), 500)

    async def ecosystem_overlaps(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import ecosystem_brain as _eb
            eco_id = request.match_info.get("eco_id")
            if eco_id:
                # Проверка владения: eco_id из пути нельзя доверять — иначе
                # любой юзер прочитает каналы и overlap чужой экосистемы.
                owns = await pool.fetchval(
                    "SELECT 1 FROM ecosystems WHERE id=$1 AND owner_id=$2",
                    int(eco_id),
                    uid,
                )
                if not owns:
                    return _err("not found", 404)
                ch_rows = await pool.fetch(
                    "SELECT channel_id FROM ecosystem_channels WHERE ecosystem_id=$1", int(eco_id)
                )
                ch_ids = [r["channel_id"] for r in ch_rows]
                if ch_ids:
                    overlaps = await _eb.analyze_audience_overlap(pool, ch_ids)
                    return _json_resp(overlaps)
            return _json_resp({"overlaps": {}})
        except Exception as e:
            return _err(str(e), 500)

    async def dashboard_realtime(request: web.Request) -> web.Response:
        """Analytics Dashboard — операторская сводка на РЕАЛЬНЫХ данных, которые
        система собирает: аккаунты, каналы, аудитория ботов, операции + дневные
        тайм-серии (аудитория/операции/успешность из bot_users и operation_audit).
        Просмотров/подписчиков каналов система не собирает — этих метрик тут нет
        (не выдумываем), вместо них показываем реальную операционную активность."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        rng = request.query.get("range", "7d")
        days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(rng, 7)
        try:
            # ── KPI (реальные) ──────────────────────────────────────────────
            acc = await _safe_fetchrow(pool,
                "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE is_active) AS active "
                "FROM tg_accounts WHERE owner_id=$1", uid)
            acc_total = int(acc["total"]) if acc else 0
            acc_active = int(acc["active"]) if acc else 0
            ch_total = int(await _safe_fetchval(pool,
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", uid) or 0)
            aud_row = await _safe_fetchrow(pool,
                """SELECT COUNT(DISTINCT bu.user_id) AS total,
                          COUNT(DISTINCT bu.user_id) FILTER (
                              WHERE bu.first_seen > NOW() - INTERVAL '7 days') AS new_7d
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE""", uid)
            aud_total = int(aud_row["total"]) if aud_row else 0
            aud_new_7d = int(aud_row["new_7d"]) if aud_row else 0
            ops_row = await _safe_fetchrow(pool,
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE action ILIKE '%post%') AS posts
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')""", uid, days)
            ops_total = int(ops_row["total"]) if ops_row else 0
            ops_posts = int(ops_row["posts"]) if ops_row else 0

            # ── Дневные тайм-серии (реальные, с заполнением пропусков нулями) ──
            def _fill(rows, key="d", val="c"):
                by_day = {}
                for r in (rows or []):
                    dd = r[key]
                    if dd is not None:
                        by_day[dd.date() if hasattr(dd, "date") else dd] = float(r[val] or 0)
                from datetime import date, timedelta
                today = date.today()
                out = []
                for i in range(days - 1, -1, -1):
                    out.append({"value": by_day.get(today - timedelta(days=i), 0)})
                return out
            aud_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', bu.first_seen) AS d, COUNT(*) AS c
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.first_seen > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)
            ops_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', occurred_at) AS d, COUNT(*) AS c
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)
            succ_series = await _safe_fetch(pool,
                """SELECT date_trunc('day', occurred_at) AS d,
                          ROUND(100.0*COUNT(*) FILTER (WHERE result='success')
                                /NULLIF(COUNT(*),0), 1) AS c
                   FROM operation_audit WHERE owner_id=$1
                     AND occurred_at > NOW() - ($2 * INTERVAL '1 day')
                   GROUP BY 1 ORDER BY 1""", uid, days)

            # ── Топ каналов по операционной активности (реально) ──────────────
            top_rows = await _safe_fetch(pool,
                """SELECT mc.title, mc.username, mc.channel_id,
                          (SELECT COUNT(*) FROM operation_audit oa
                           WHERE oa.owner_id=$1 AND oa.target IS NOT NULL
                             AND (oa.target = mc.username OR oa.target = mc.channel_id::text)
                          ) AS activity
                   FROM managed_channels mc WHERE mc.owner_id=$1
                   ORDER BY activity DESC, mc.added_at DESC LIMIT 10""", uid)
            top_channels = [{
                "name": r["title"] or r["username"] or str(r["channel_id"]),
                "username": r["username"] or "",
                "subscribers": int(r["activity"] or 0),  # операц. активность канала
                "views": 0, "growth": 0,
            } for r in (top_rows or [])]

            act_rows = await _safe_fetch(pool,
                "SELECT action, result, target, occurred_at FROM operation_audit "
                "WHERE owner_id=$1 ORDER BY occurred_at DESC LIMIT 15", uid)
            def _atype(a: str) -> str:
                a = (a or "").lower()
                if "post" in a: return "post"
                if "join" in a or "sub" in a or "invite" in a: return "sub"
                if "leave" in a or "unsub" in a: return "unsub"
                return "other"
            recent_activity = [{
                "type": _atype(r["action"]),
                "text": f"{r['action']}"
                        + (f" → {r['target']}" if r["target"] else "")
                        + (f" ({r['result']})" if r["result"] and r["result"] != "success" else ""),
                "timestamp": r["occurred_at"].isoformat() if r["occurred_at"] else None,
            } for r in (act_rows or [])]

            return _json_resp({
                # KPI (реальные) — фронт-плитки перелейблены под них
                "total_subscribers": aud_total,      # 👥 Аудитория
                "total_channels": ch_total,          # 📡 Каналы
                "total_posts": ops_posts,            # 📝 Посты (постинг-операции)
                "total_accounts": acc_total,         # 📱 Аккаунты
                "active_accounts": acc_active,
                "total_operations": ops_total,       # ⚡ Операций за период
                "total_views": ops_total,            # (совместимость со старой плиткой)
                "growth_7d": aud_new_7d,             # 📈 Новая аудитория 7д
                # Тайм-серии (реальные, заполнены по дням)
                "subs_history": _fill(aud_series),          # Аудитория/день
                "views_history": _fill(ops_series),         # Операции/день
                "engagement_history": _fill(succ_series),   # Успешность %/день
                "top_channels": top_channels,
                "recent_activity": recent_activity,
                "range": rng,
            })
        except Exception as e:
            log.exception("dashboard_realtime uid=%s", uid)
            return _err(str(e)[:150], 500)

    async def audience_analytics(request: web.Request) -> web.Response:
        """Audience Analytics — owner-агрегат аудитории ботов в форме экрана
        s-audience-analytics. Сегменты — по активности пользователей ботов
        владельца; heatmap — по часам активности (реальные данные из bot_users)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            total = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE""", uid) or 0
            active = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.is_active=TRUE
                     AND bu.last_seen > NOW() - INTERVAL '7 days'""", uid) or 0
            total = int(total); active = int(active)
            new_7d = await _safe_fetchval(pool,
                """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
                   JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.first_seen > NOW() - INTERVAL '7 days'""", uid) or 0
            dormant = max(total - active, 0)
            def _seg(name, count, color):
                return {"name": name, "count": int(count),
                        "percentage": round(count / total * 100, 1) if total else 0.0,
                        "growth_pct": None, "avg_activity": "", "color": color}
            segments = [
                _seg("Активные (7д)", active, "#34c759"),
                _seg("Новые (7д)", int(new_7d), "#0a84ff"),
                _seg("Спящие", dormant, "#ff9f0a"),
            ] if total else []
            avg_eng = round(active / total * 100, 1) if total else 0.0
            hm_rows = await _safe_fetch(pool,
                """SELECT EXTRACT(HOUR FROM bu.last_seen)::int AS h, COUNT(*) AS c
                   FROM bot_users bu JOIN managed_bots mb ON mb.bot_id=bu.bot_id
                   WHERE mb.added_by=$1 AND bu.last_seen IS NOT NULL
                   GROUP BY 1 ORDER BY 1""", uid)
            heatmap = [{"hour": int(r["h"]), "value": int(r["c"])} for r in (hm_rows or [])]
            insights = []
            if total:
                insights.append({"title": "Аудитория", "text":
                    f"Всего {total}, активных за 7д — {active} ({avg_eng}%)."})
                if dormant:
                    insights.append({"title": "Реактивация", "text":
                        f"{dormant} спящих — кандидаты на прогрев/рассылку."})
            return _json_resp({
                "total_users": total, "active_users": active,
                "avg_engagement": avg_eng, "segments": segments,
                "insights": insights, "heatmap": heatmap,
            })
        except Exception as e:
            log.exception("audience_analytics uid=%s", uid)
            return _err(str(e)[:150], 500)

    async def networks_list(request: web.Request) -> web.Response:
        """Network Builder — список сеток владельца (форма экрана s-network-builder)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await _safe_fetch(pool,
                """SELECT ni.id, ni.name, COALESCE(ni.status,'active') AS status,
                     (SELECT COUNT(*) FROM network_nodes n WHERE n.instance_id=ni.id) AS node_count,
                     (SELECT COUNT(*) FROM network_edges e WHERE e.instance_id=ni.id) AS edge_count
                   FROM network_instances ni WHERE ni.owner_id=$1 ORDER BY ni.created_at DESC""",
                uid)
            return _json_resp({"networks": [dict(r) for r in (rows or [])]})
        except Exception as e:
            log.exception("networks_list uid=%s", uid)
            return _err(str(e)[:150], 500)

    async def network_create_plural(request: web.Request) -> web.Response:
        """Создать сетку. body: {name, template?, description?}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        name = validate_string(body.get("name"), max_len=100)
        if not name:
            return _err("Укажите название сети", 400)
        try:
            nid = await pool.fetchval(
                "INSERT INTO network_instances(owner_id, name, status) "
                "VALUES($1,$2,'active') RETURNING id", uid, name)
            return _json_resp({"ok": True, "id": nid})
        except Exception as e:
            log.exception("network_create uid=%s", uid)
            return _err(str(e)[:150], 500)

    async def network_detail_plural(request: web.Request) -> web.Response:
        """Детали сети: узлы/рёбра в форме, которую рисует граф (from_id/to_id/labels)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
        except (KeyError, ValueError):
            return _err("bad id", 400)
        from services import network_builder as _nb
        d = await _nb.get_instance_detail(pool, uid, nid)
        if not d:
            return _err("Сеть не найдена", 404)
        label_by_id = {n["id"]: (n.get("label") or n.get("node_type") or "#") for n in d["nodes"]}
        nodes = [{"id": n["id"], "type": n.get("node_type"),
                  "label": n.get("label") or "", "object_id": n.get("ref_id")}
                 for n in d["nodes"]]
        edges = [{"id": e["id"], "from_id": e.get("source_node_id"),
                  "to_id": e.get("target_node_id"), "type": e.get("edge_type"),
                  "from_label": label_by_id.get(e.get("source_node_id"), "#"),
                  "to_label": label_by_id.get(e.get("target_node_id"), "#")}
                 for e in d["edges"]]
        inst = d["instance"]
        return _json_resp({"id": nid, "name": inst.get("name"),
                           "status": inst.get("status"), "nodes": nodes, "edges": edges})

    async def network_add_node(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM network_instances WHERE id=$1 AND owner_id=$2", nid, uid)
        if not owns:
            return _err("Сеть не найдена", 404)
        from services import network_builder as _nb
        typ = validate_string(body.get("type"), max_len=40) or "node"
        label = validate_string(body.get("label"), max_len=200) or ""
        try:
            ref = int(body.get("object_id")) if str(body.get("object_id") or "").strip() else None
        except (TypeError, ValueError):
            ref = None
        res = await _nb.add_node(pool, nid, typ, label, ref_id=ref)
        return _json_resp(res)

    async def network_delete_node(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            node_id = int(request.match_info["node_id"])
        except (KeyError, ValueError):
            return _err("bad id", 400)
        await pool.execute(
            "DELETE FROM network_nodes WHERE id=$1 AND instance_id IN "
            "(SELECT id FROM network_instances WHERE owner_id=$2)", node_id, uid)
        return _json_resp({"ok": True})

    async def network_add_edge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            nid = int(request.match_info["net_id"])
            body = await request.json()
            frm = int(body.get("from_id")); to = int(body.get("to_id"))
        except Exception:
            return _err("bad request", 400)
        owns = await _safe_count(pool,
            "SELECT COUNT(*) FROM network_instances WHERE id=$1 AND owner_id=$2", nid, uid)
        if not owns:
            return _err("Сеть не найдена", 404)
        from services import network_builder as _nb
        typ = validate_string(body.get("type"), max_len=40) or "forward"
        res = await _nb.add_edge(pool, nid, frm, to, edge_type=typ)
        return _json_resp(res)

    async def network_delete_edge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            edge_id = int(request.match_info["edge_id"])
        except (KeyError, ValueError):
            return _err("bad id", 400)
        await pool.execute(
            "DELETE FROM network_edges WHERE id=$1 AND instance_id IN "
            "(SELECT id FROM network_instances WHERE owner_id=$2)", edge_id, uid)
        return _json_resp({"ok": True})

    async def workflow_create_plural(request: web.Request) -> web.Response:
        """Создать воркфлоу (контракт экрана: POST /workflows {name,bot_id?,description?})."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        name = validate_string(body.get("name"), max_len=120)
        if not name:
            return _err("Укажите название воркфлоу", 400)
        desc = validate_string(body.get("description"), max_len=500) or None
        try:
            wid = await pool.fetchval(
                "INSERT INTO workflow_definitions(owner_id, name, description, steps, is_active) "
                "VALUES($1,$2,$3,'[]'::jsonb, FALSE) RETURNING id", uid, name, desc)
            return _json_resp({"ok": True, "id": wid})
        except Exception as e:
            log.exception("workflow_create_plural uid=%s", uid)
            return _err(str(e)[:150], 500)

    async def workflow_detail_plural(request: web.Request) -> web.Response:
        """Детали воркфлоу: {id,name,active,steps[]} (шаги хранятся inline jsonb)."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad id", 400)
        row = await _safe_fetchrow(pool,
            "SELECT id, name, description, steps, COALESCE(is_active,FALSE) AS is_active "
            "FROM workflow_definitions WHERE id=$1 AND owner_id=$2", wid, uid)
        if not row:
            return _err("Воркфлоу не найден", 404)
        steps = row["steps"]
        if isinstance(steps, str):
            try:
                steps = _json.loads(steps)
            except Exception:
                steps = []
        return _json_resp({"id": row["id"], "name": row["name"],
                           "description": row["description"] or "",
                           "active": bool(row["is_active"]), "steps": steps or []})

    async def workflow_toggle_plural(request: web.Request) -> web.Response:
        """Активировать/поставить на паузу: PATCH /workflows/{id} {active}."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["wf_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        active = bool(body.get("active"))
        res = await pool.execute(
            "UPDATE workflow_definitions SET is_active=$1, updated_at=NOW() "
            "WHERE id=$2 AND owner_id=$3", active, wid, uid)
        if isinstance(res, str) and res.endswith(" 0"):
            return _err("Воркфлоу не найден", 404)
        return _json_resp({"ok": True, "active": active})

    async def workflow_delete_plural(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad id", 400)
        await pool.execute(
            "DELETE FROM workflow_definitions WHERE id=$1 AND owner_id=$2", wid, uid)
        return _json_resp({"ok": True})

    async def workflow_add_step(request: web.Request) -> web.Response:
        """Добавить шаг: POST /workflows/{id}/steps {type,...} — аппенд в steps jsonb."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wid = int(request.match_info["wf_id"])
            body = await request.json()
        except Exception:
            return _err("bad request", 400)
        if not isinstance(body, dict) or not body.get("type"):
            return _err("Шаг должен содержать type", 400)
        res = await pool.execute(
            "UPDATE workflow_definitions SET steps = COALESCE(steps,'[]'::jsonb) || $1::jsonb, "
            "updated_at=NOW() WHERE id=$2 AND owner_id=$3",
            _json.dumps([body]), wid, uid)
        if isinstance(res, str) and res.endswith(" 0"):
            return _err("Воркфлоу не найден", 404)
        return _json_resp({"ok": True})

    async def operation_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        fmt = request.query.get("format", "csv")
        try:
            rows = await pool.fetch(
                """SELECT id, op_type, label, status, total_items, done_items,
                          err_cnt, created_at, finished_at
                   FROM operation_queue WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 500""",
                uid,
            )
            if fmt == "json":
                return _json_resp({"operations": [dict(r) for r in rows]})
            header = ["ID", "Тип", "Метка", "Статус", "Всего", "Сделано", "Ошибок", "Создано", "Завершено"]
            data = [[
                r["id"], r["op_type"], r["label"] or "", r["status"],
                r["total_items"] or 0, r["done_items"] or 0, r["err_cnt"] or 0,
                str(r["created_at"] or ""), str(r["finished_at"] or ""),
            ] for r in rows]
            return _csv_resp("operations.csv", header, data)
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/circuit_breaker", circuit_breaker_status)
    app.router.add_get("/api/miniapp/proxy_stats", proxy_stats)
    app.router.add_get("/api/miniapp/dashboard_realtime", dashboard_realtime)
    app.router.add_get("/api/miniapp/audience_analytics", audience_analytics)
    app.router.add_get("/api/miniapp/networks", networks_list)
    app.router.add_post("/api/miniapp/networks", network_create_plural)
    app.router.add_delete("/api/miniapp/networks/nodes/{node_id}", network_delete_node)
    app.router.add_delete("/api/miniapp/networks/edges/{edge_id}", network_delete_edge)
    app.router.add_get("/api/miniapp/networks/{net_id}", network_detail_plural)
    app.router.add_post("/api/miniapp/networks/{net_id}/nodes", network_add_node)
    app.router.add_post("/api/miniapp/networks/{net_id}/edges", network_add_edge)
    app.router.add_get("/api/miniapp/ecosystem_recommendations", ecosystem_recommendations)
    app.router.add_get("/api/miniapp/ecosystem/{eco_id}/overlaps", ecosystem_overlaps)
    app.router.add_get("/api/miniapp/operations/export", operation_export)

    # Team & Audit
    async def team_members(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Только команда самого пользователя: он сам + участники рабочих
            # пространств, которыми он владеет или в которых состоит.
            # НЕЛЬЗЯ отдавать весь список platform_users — это утечка данных.
            # platform_users не имеет created_at/last_active_at — реальные колонки
            # registered_at/last_seen (schema_v39). Раньше этот запрос падал
            # `column "created_at" does not exist` и экран «Команда» отдавал 500.
            # Алиасим, чтобы фронт-контракт (created_at/last_active_at) не менялся.
            rows = await pool.fetch(
                """SELECT DISTINCT pu.user_id, pu.username, pu.first_name,
                          pu.current_plan,
                          pu.registered_at AS created_at,
                          pu.last_seen AS last_active_at
                   FROM platform_users pu
                   WHERE pu.user_id = $1
                      OR pu.user_id IN (
                          SELECT wm.user_id
                          FROM workspace_members wm
                          JOIN workspaces w ON w.id = wm.workspace_id
                          WHERE w.owner_id = $1
                             OR w.id IN (
                                 SELECT workspace_id FROM workspace_members
                                 WHERE user_id = $1
                             )
                      )
                   ORDER BY last_active_at DESC NULLS LAST
                   LIMIT 50""",
                uid,
            )
            return _json_resp({"members": [dict(r) for r in rows]})
        except Exception as e:
            return _err(str(e), 500)

    async def audit_trail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            # Только операции самого пользователя (oq.owner_id = uid).
            # Без этого фильтра юзер видел бы аудит ВСЕХ владельцев — утечка.
            rows = await pool.fetch(
                "SELECT ol.op_id, ol.step_num, ol.target, ol.status, ol.message, ol.created_at, "
                "oq.owner_id, oq.op_type, oq.label "
                "FROM operation_log ol JOIN operation_queue oq ON oq.id = ol.op_id "
                "WHERE oq.owner_id = $1 AND ol.created_at > NOW() - INTERVAL '7 days' "
                "ORDER BY ol.created_at DESC LIMIT 200",
                uid,
            )
            return _json_resp({"entries": [dict(r) for r in rows]})
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/team/members", team_members)
    app.router.add_get("/api/miniapp/audit", audit_trail)

    # ── Session Import ───────────────────────────────────────────────────────

    async def import_sessions_api(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            raw = data.get("sessions", "")
            proxy = data.get("proxy_url")
            from services.session_importer import import_sessions
            result = await import_sessions(pool, uid, raw, proxy)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_post("/api/miniapp/import_sessions", import_sessions_api)

    async def team_audit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid): return _err("Forbidden", 403)
        try:
            rows = await pool.fetch(
                """SELECT ol.op_id, ol.target, ol.status, ol.message, ol.created_at,
                          oq.owner_id, oq.op_type, oq.label
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE ol.created_at > NOW() - INTERVAL '7 days'
                   ORDER BY ol.created_at DESC LIMIT 200"""
            )
            return _json_resp({"entries": [dict(r) for r in rows]})
        except Exception as e:
            return _err(str(e), 500)

    async def user_activity_log(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                """SELECT ol.target, ol.status, ol.message, ol.created_at, oq.op_type
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE oq.owner_id = $1
                   ORDER BY ol.created_at DESC LIMIT 50""",
                uid
            )
            return _json_resp({"log": [dict(r) for r in rows]})
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/team/audit", team_audit)
    app.router.add_get("/api/miniapp/my_activity", user_activity_log)

    # ── Unified Contacts Hub ──────────────────────────────────────────────────

    async def uch_contacts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        search = request.query.get('search', '')
        tag = request.query.get('tag')
        favorite = request.query.get('favorite') == '1'
        premium = request.query.get('premium') == '1'
        multi = request.query.get('multi') == '1'
        mutual = request.query.get('mutual') == '1'
        try:
            from services.contacts_hub.repository import get_contacts
            result = await get_contacts(pool, uid, search=search, favorite_only=favorite,
                                        tag=tag, premium_only=premium, multi_only=multi,
                                        mutual_only=mutual)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_contact_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            from services.contacts_hub.repository import get_contact
            result = await get_contact(pool, contact_id, uid)
            if not result: return _err("Not found", 404)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_contact_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.repository import update_contact, log_contact_history
            success = await update_contact(pool, contact_id, uid, data)
            if not success: return _err("Not found", 404)
            await log_contact_history(pool, contact_id, uid, 'edit', source='user')
            return _json_resp({'ok': True})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_contact_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            contact_id = request.match_info['contact_id']
            from services.contacts_hub.repository import delete_contact
            success = await delete_contact(pool, contact_id, uid)
            if not success: return _err("Not found", 404)
            return _json_resp({'ok': True})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_search(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        query = request.query.get('q', '')
        if not query: return _err("Query required", 400)
        try:
            from services.contacts_hub.search_engine import search_contacts
            results = await search_contacts(pool, uid, query)
            return _json_resp({'results': results, 'total': len(results)})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.stats_engine import get_full_stats, get_account_stats
            stats = await get_full_stats(pool, uid)
            account_stats = await get_account_stats(pool, uid)
            return _json_resp({**stats, 'account_stats': account_stats})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_sync(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.sync_service import sync_all_accounts
            result = await sync_all_accounts(pool, uid)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_groups(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.repository import get_contact_groups
            groups = await get_contact_groups(pool, uid)
            return _json_resp({'groups': groups})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_group_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import create_group
            gid = await create_group(pool, uid, data.get('name', ''), data.get('color'))
            return _json_resp({'ok': True, 'id': gid})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_group_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            gid = int(request.match_info['group_id'])
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import update_group
            ok = await update_group(pool, gid, uid, data.get('name'), data.get('color'))
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_group_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            gid = int(request.match_info['group_id'])
            from services.contacts_hub.bulk_ops_engine import delete_group
            ok = await delete_group(pool, gid, uid)
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_toggle_favorite(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.repository import update_contact
            row = await pool.fetchrow('SELECT is_favorite FROM unified_contacts WHERE id=$1 AND owner_id=$2', cid, uid)
            if not row: return _err("Not found", 404)
            new_val = not row['is_favorite']
            await update_contact(pool, cid, uid, {'is_favorite': new_val})
            return _json_resp({'ok': True, 'is_favorite': new_val})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_add_to_group(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            gid = int(request.match_info['group_id'])
            from services.contacts_hub.repository import add_contact_to_group
            ok = await add_contact_to_group(pool, cid, gid)
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_remove_from_group(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            gid = int(request.match_info['group_id'])
            from services.contacts_hub.repository import remove_contact_from_group
            ok = await remove_contact_from_group(pool, cid, gid)
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_contact_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            rows = await pool.fetch(
                'SELECT * FROM contact_history WHERE contact_id=$1 AND owner_id=$2 ORDER BY created_at DESC LIMIT 100',
                cid, uid)
            return _json_resp({'history': [dict(r) for r in rows]})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_contact_versions(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.versioning_engine import get_versions
            versions = await get_versions(pool, cid, uid)
            return _json_resp({'versions': versions})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_rollback(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            vnum = int(request.match_info['version_num'])
            from services.contacts_hub.versioning_engine import rollback_to_version
            ok = await rollback_to_version(pool, cid, uid, vnum)
            if not ok: return _err("Version not found", 404)
            return _json_resp({'ok': True})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_timeline(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            history = await pool.fetch(
                'SELECT action, field_name, old_value, new_value, source, created_at FROM contact_history WHERE contact_id=$1 AND owner_id=$2 ORDER BY created_at DESC LIMIT 50',
                cid, uid)
            return _json_resp({'timeline': [dict(r) for r in history]})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_relationships(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.relationship_engine import get_relationships
            rels = await get_relationships(pool, uid, cid)
            return _json_resp({'relationships': rels})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_identity(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.identity_engine import build_identity_graph, get_last_active
            graph = await build_identity_graph(pool, uid, cid)
            last_active = await get_last_active(pool, cid)
            return _json_resp({**graph, 'last_active': last_active})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_crm_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.crm_engine import get_crm_data, get_crm_activity
            crm = await get_crm_data(pool, uid, cid)
            activity = await get_crm_activity(pool, uid, cid)
            return _json_resp({'crm': crm, 'activity': activity})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_crm_upsert(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.crm_engine import upsert_crm
            crm = await upsert_crm(pool, uid, cid, data)
            return _json_resp({'ok': True, 'crm': crm})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_crm_activity(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.crm_engine import log_crm_activity
            await log_crm_activity(pool, uid, cid, data.get('type', 'note'),
                                   data.get('description'), data.get('metadata'))
            return _json_resp({'ok': True})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_crm_activity_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            from services.contacts_hub.crm_engine import get_crm_activity
            activity = await get_crm_activity(pool, uid, cid)
            return _json_resp({'activity': activity})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_crm_reminder(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = request.match_info['contact_id']
            data = await request.json()
            from services.contacts_hub.crm_engine import upsert_crm
            await upsert_crm(pool, uid, cid, {
                'next_reminder_at': data.get('remind_at'),
                'next_reminder_text': data.get('text', ''),
            })
            return _json_resp({'ok': True})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_duplicates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import detect_smart_duplicates
            dupes = await detect_smart_duplicates(pool, uid)
            return _json_resp({'duplicates': dupes, 'count': len(dupes)})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_merge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            primary_id = data.get('primary_id')
            secondary_id = data.get('secondary_id')
            if not primary_id or not secondary_id:
                return _err("primary_id and secondary_id required", 400)
            from services.contacts_hub.merge_engine import manual_merge
            result = await manual_merge(pool, primary_id, secondary_id, uid)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_conflicts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import get_conflicts
            conflicts = await get_conflicts(pool, uid)
            return _json_resp({'conflicts': conflicts})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_resolve_conflict(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            cid = int(request.match_info['conflict_id'])
            data = await request.json()
            from services.contacts_hub.trust_engine import resolve_conflict
            ok = await resolve_conflict(pool, cid, uid, data.get('resolution', 'accepted'),
                                        data.get('value'), uid)
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tags(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import get_smart_tags
            tags = await get_smart_tags(pool, uid)
            return _json_resp({'tags': tags})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tag_rules(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import get_smart_tag_rules
            rules = await get_smart_tag_rules(pool, uid)
            return _json_resp({'rules': rules})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tags_apply(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.smart_tags_engine import apply_smart_tags
            result = await apply_smart_tags(pool, uid)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tag_rule_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.smart_tags_engine import create_smart_tag_rule
            rid = await create_smart_tag_rule(pool, uid, data.get('name', ''),
                                              data.get('tag', ''), data.get('conditions', {}))
            return _json_resp({'ok': True, 'id': rid})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tag_rule_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info['rule_id'])
            from services.contacts_hub.smart_tags_engine import delete_smart_tag_rule
            ok = await delete_smart_tag_rule(pool, rid, uid)
            return _json_resp({'ok': ok})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_smart_tag_rule_toggle(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info['rule_id'])
            from services.contacts_hub.smart_tags_engine import toggle_smart_tag_rule
            active = await toggle_smart_tag_rule(pool, rid, uid)
            return _json_resp({'ok': True, 'is_active': active})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_tag(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_tag
            result = await bulk_tag(pool, uid, data.get('contact_ids', []), data.get('tag', ''))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_untag(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_untag
            result = await bulk_untag(pool, uid, data.get('contact_ids', []), data.get('tag', ''))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_favorite(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_set_favorite
            result = await bulk_set_favorite(pool, uid, data.get('contact_ids', []), data.get('is_favorite', True))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.contacts_hub.bulk_ops_engine import bulk_delete
            result = await bulk_delete(pool, uid, data.get('contact_ids', []))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_group(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            gid = data.get('group_id')
            if not gid: return _err("group_id required", 400)
            action = data.get('action', 'add')
            if action == 'add':
                from services.contacts_hub.bulk_ops_engine import bulk_add_to_group
                result = await bulk_add_to_group(pool, uid, data.get('contact_ids', []), gid)
            else:
                from services.contacts_hub.bulk_ops_engine import bulk_remove_from_group
                result = await bulk_remove_from_group(pool, uid, data.get('contact_ids', []), gid)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_merge(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            pairs = data.get('pairs', [])
            if not pairs: return _err("pairs required", 400)
            from services.contacts_hub.bulk_ops_engine import bulk_merge
            result = await bulk_merge(pool, uid, pairs)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            contact_ids = data.get('contact_ids', [])
            fmt = data.get('format', 'json')
            from services.contacts_hub.bulk_ops_engine import bulk_export
            result = await bulk_export(pool, uid, contact_ids, fmt)
            content_types = {'csv': 'text/csv', 'vcf': 'text/vcard', 'json': 'application/json'}
            return web.Response(
                body=result['data'], content_type=content_types.get(fmt, 'application/json'),
                headers={'Content-Disposition': f'attachment; filename="contacts.{fmt}"'})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_importance(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            contact_ids = data.get('contact_ids', [])
            level = data.get('level', 0)
            from services.contacts_hub.bulk_ops_engine import bulk_set_importance
            result = await bulk_set_importance(pool, uid, contact_ids, level)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_bulk_rating(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            contact_ids = data.get('contact_ids', [])
            rating = data.get('rating', 0)
            from services.contacts_hub.bulk_ops_engine import bulk_set_rating
            result = await bulk_set_rating(pool, uid, contact_ids, rating)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_export_csv(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.export_engine import export_csv
            csv_data = await export_csv(pool, uid)
            return web.Response(body=csv_data, content_type='text/csv',
                                headers={'Content-Disposition': 'attachment; filename="contacts.csv"'})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_export_vcf(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.export_engine import export_vcf
            vcf_data = await export_vcf(pool, uid)
            return web.Response(body=vcf_data, content_type='text/vcard',
                                headers={'Content-Disposition': 'attachment; filename="contacts.vcf"'})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_export_json(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.export_engine import export_json
            json_data = await export_json(pool, uid)
            return web.Response(body=json_data, content_type='application/json',
                                headers={'Content-Disposition': 'attachment; filename="contacts.json"'})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_reminders(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.crm_engine import get_upcoming_reminders, get_crm_overdue
            upcoming = await get_upcoming_reminders(pool, uid)
            overdue = await get_crm_overdue(pool, uid)
            return _json_resp({'upcoming': upcoming, 'overdue': overdue})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_graph_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.relationship_engine import get_graph_stats
            stats = await get_graph_stats(pool, uid)
            return _json_resp(stats)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_graph_compute(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.relationship_engine import compute_relationships
            result = await compute_relationships(pool, uid)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_trust_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.contacts_hub.trust_engine import update_trust_scores
            updated = await update_trust_scores(pool, uid)
            return _json_resp({'updated': updated})
        except Exception as e:
            return _err(str(e), 500)

    async def uch_ai_query(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            query = data.get('query', '').strip()
            if not query: return _err("Query required", 400)
            from services.contacts_hub.ai_assistant import process_ai_query
            result = await process_ai_query(pool, uid, query)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def uch_spotlight(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        q = request.query.get('q', '').strip()
        if not q: return _json_resp({'results': []})
        try:
            from services.contacts_hub.search_engine import search_contacts_spotlight
            results = await search_contacts_spotlight(pool, uid, q)
            return _json_resp({'results': results, 'total': len(results)})
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/uch/contacts", uch_contacts)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}", uch_contact_detail)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}", uch_contact_update)
    app.router.add_delete("/api/miniapp/uch/contacts/{contact_id}", uch_contact_delete)
    app.router.add_get("/api/miniapp/uch/search", uch_search)
    app.router.add_get("/api/miniapp/uch/stats", uch_stats)
    app.router.add_post("/api/miniapp/uch/sync", uch_sync)
    app.router.add_get("/api/miniapp/uch/groups", uch_groups)
    app.router.add_post("/api/miniapp/uch/groups", uch_group_create)
    app.router.add_put("/api/miniapp/uch/groups/{group_id}", uch_group_update)
    app.router.add_delete("/api/miniapp/uch/groups/{group_id}", uch_group_delete)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/favorite", uch_toggle_favorite)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/groups/{group_id}", uch_add_to_group)
    app.router.add_delete("/api/miniapp/uch/contacts/{contact_id}/groups/{group_id}", uch_remove_from_group)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/history", uch_contact_history)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/versions", uch_contact_versions)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/rollback/{version_num}", uch_rollback)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/timeline", uch_timeline)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/relationships", uch_relationships)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/identity", uch_identity)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/crm", uch_crm_get)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm", uch_crm_upsert)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm/activity", uch_crm_activity)
    app.router.add_get("/api/miniapp/uch/contacts/{contact_id}/crm/activity", uch_crm_activity_list)
    app.router.add_post("/api/miniapp/uch/contacts/{contact_id}/crm/reminder", uch_crm_reminder)
    app.router.add_get("/api/miniapp/uch/duplicates", uch_duplicates)
    app.router.add_post("/api/miniapp/uch/merge", uch_merge)
    app.router.add_get("/api/miniapp/uch/conflicts", uch_conflicts)
    app.router.add_post("/api/miniapp/uch/conflicts/{conflict_id}/resolve", uch_resolve_conflict)
    app.router.add_get("/api/miniapp/uch/smart-tags", uch_smart_tags)
    app.router.add_get("/api/miniapp/uch/smart-tags/rules", uch_smart_tag_rules)
    app.router.add_post("/api/miniapp/uch/smart-tags/apply", uch_smart_tags_apply)
    app.router.add_post("/api/miniapp/uch/smart-tags/rules", uch_smart_tag_rule_create)
    app.router.add_delete("/api/miniapp/uch/smart-tags/rules/{rule_id}", uch_smart_tag_rule_delete)
    app.router.add_put("/api/miniapp/uch/smart-tags/rules/{rule_id}/toggle", uch_smart_tag_rule_toggle)
    app.router.add_post("/api/miniapp/uch/bulk/tag", uch_bulk_tag)
    app.router.add_post("/api/miniapp/uch/bulk/untag", uch_bulk_untag)
    app.router.add_post("/api/miniapp/uch/bulk/favorite", uch_bulk_favorite)
    app.router.add_post("/api/miniapp/uch/bulk/delete", uch_bulk_delete)
    app.router.add_post("/api/miniapp/uch/bulk/group", uch_bulk_group)
    app.router.add_post("/api/miniapp/uch/bulk/merge", uch_bulk_merge)
    app.router.add_post("/api/miniapp/uch/bulk/export", uch_bulk_export)
    app.router.add_post("/api/miniapp/uch/bulk/importance", uch_bulk_importance)
    app.router.add_post("/api/miniapp/uch/bulk/rating", uch_bulk_rating)
    app.router.add_get("/api/miniapp/uch/export/csv", uch_export_csv)
    app.router.add_get("/api/miniapp/uch/export/vcf", uch_export_vcf)
    app.router.add_get("/api/miniapp/uch/export/json", uch_export_json)
    app.router.add_get("/api/miniapp/uch/reminders", uch_reminders)
    app.router.add_get("/api/miniapp/uch/graph/stats", uch_graph_stats)
    app.router.add_post("/api/miniapp/uch/graph/compute", uch_graph_compute)
    app.router.add_post("/api/miniapp/uch/trust/update", uch_trust_update)
    app.router.add_post("/api/miniapp/uch/ai", uch_ai_query)
    app.router.add_get("/api/miniapp/uch/spotlight", uch_spotlight)

    # ── CF Pool Manager ──────────────────────────────────────────────────────
    async def cf_pool_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.cf_pool_manager import get_pool_status
            status = await get_pool_status(pool, uid)
            return _json_resp(status)
        except Exception as e:
            return _err(str(e), 500)

    async def cf_pool_deploy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            count = int(data.get('count', 5))
            prefix = data.get('name_prefix', f'tg-relay-{uid}')
            import os
            api_token = os.getenv('CF_API_TOKEN', '')
            account_id = os.getenv('CF_ACCOUNT_ID', '')
            if not api_token or not account_id:
                return _err("CF_API_TOKEN and CF_ACCOUNT_ID required", 400)
            from services.cf_pool_manager import deploy_pool, assign_urls_to_accounts
            urls = await deploy_pool(count, prefix, api_token, account_id)
            if urls:
                result = await assign_urls_to_accounts(pool, uid, urls)
                return _json_resp({"ok": True, **result})
            return _err("No workers deployed", 500)
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/cf/pool/status", cf_pool_status)
    app.router.add_post("/api/miniapp/cf/pool/deploy", cf_pool_deploy)

    # ── Search Ranking Engine ──────────────────────────────────────────────────
    async def ranking_track(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import track_keyword
            result = await track_keyword(pool, uid, data.get('keyword', ''),
                                         data.get('channel_id'), data.get('check_interval', 3600))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_untrack(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import untrack_keyword
            result = await untrack_keyword(pool, uid, data.get('keyword', ''), data.get('channel_id'))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_record(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.ranking_engine import record_position
            result = await record_position(pool, uid, data.get('channel_id', 0),
                                           data.get('keyword', ''), data.get('position', 0))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_history(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            channel_id = int(request.match_info.get('channel_id', 0))
            keyword = request.query.get('keyword', '')
            days = int(request.query.get('days', 30))
            from services.ranking_engine import get_position_history
            history = await get_position_history(pool, uid, channel_id, keyword, days)
            return _json_resp({'history': history})
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_positions(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_all_positions
            positions = await get_all_positions(pool, uid)
            return _json_resp({'positions': positions})
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_keywords(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_tracked_keywords
            keywords = await get_tracked_keywords(pool, uid)
            return _json_resp({'keywords': keywords})
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_overview(request: web.Request) -> web.Response:
        """Свод для экрана «Рейтинг»: слитые отслеживаемые ключи + их последние
        позиции (position/trend) + алерты. Фронт (loadRanking) звал bare
        /api/miniapp/ranking, но такого маршрута не было → экран не грузил данные
        (404). Собираем из готовых функций ranking_engine — без дублей.
        trend = previous_position - position (положительный = рост позиции)."""
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import (
                get_tracked_keywords, get_all_positions, get_alerts)
            tracked = await get_tracked_keywords(pool, uid)
            positions = await get_all_positions(pool, uid)
            alerts = await get_alerts(pool, uid)
            # позиции по ключу (последняя на канал/ключ)
            pos_by_kw = {}
            for p in positions:
                pos_by_kw[(p.get('keyword'), p.get('channel_id'))] = p
            keywords = []
            for k in tracked:
                p = pos_by_kw.get((k.get('keyword'), k.get('channel_id')))
                cur = p.get('position') if p else None
                prev = p.get('previous_position') if p else None
                trend = (prev - cur) if (cur is not None and prev) else 0
                keywords.append({
                    'keyword': k.get('keyword'),
                    'channel_id': k.get('channel_id'),
                    'position': cur,
                    'trend': trend,
                    'region': k.get('region') or 'ru',
                })
            return _json_resp({'keywords': keywords, 'alerts': alerts})
        except Exception as e:
            log.exception("ranking_overview uid=%d", uid)
            return _err(str(e), 500)

    async def ranking_alerts(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_alerts
            alerts = await get_alerts(pool, uid)
            return _json_resp({'alerts': alerts})
        except Exception as e:
            return _err(str(e), 500)

    async def ranking_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.ranking_engine import get_ranking_stats
            stats = await get_ranking_stats(pool, uid)
            return _json_resp(stats)
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_post("/api/miniapp/ranking/track", ranking_track)
    app.router.add_post("/api/miniapp/ranking/untrack", ranking_untrack)
    app.router.add_post("/api/miniapp/ranking/record", ranking_record)
    app.router.add_get("/api/miniapp/ranking/history/{channel_id}", ranking_history)
    app.router.add_get("/api/miniapp/ranking/positions", ranking_positions)
    app.router.add_get("/api/miniapp/ranking", ranking_overview)
    app.router.add_get("/api/miniapp/ranking/keywords", ranking_keywords)
    app.router.add_get("/api/miniapp/ranking/alerts", ranking_alerts)
    app.router.add_get("/api/miniapp/ranking/stats", ranking_stats)

    # ── Network Builder ──────────────────────────────────────────────────────
    async def network_templates(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_templates
            templates = await get_templates(pool, uid)
            return _json_resp({'templates': templates})
        except Exception as e:
            return _err(str(e), 500)

    async def network_template_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.network_builder import create_template
            result = await create_template(pool, uid, data.get('name', ''),
                                           data.get('description', ''),
                                           data.get('template_type', 'channel_group'),
                                           data.get('nodes'), data.get('edges'))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def network_template_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            template_id = int(request.match_info['template_id'])
            from services.network_builder import delete_template
            result = await delete_template(pool, uid, template_id)
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def network_instances(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_instances
            instances = await get_instances(pool, uid)
            return _json_resp({'instances': instances})
        except Exception as e:
            return _err(str(e), 500)

    async def network_instance_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            data = await request.json()
            from services.network_builder import create_instance
            result = await create_instance(pool, uid, data.get('template_id', 0),
                                           data.get('name', ''))
            return _json_resp(result)
        except Exception as e:
            return _err(str(e), 500)

    async def network_instance_detail(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            instance_id = int(request.match_info['instance_id'])
            from services.network_builder import get_instance_detail
            detail = await get_instance_detail(pool, uid, instance_id)
            if not detail:
                return _err("Not found", 404)
            return _json_resp(detail)
        except Exception as e:
            return _err(str(e), 500)

    async def network_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.network_builder import get_network_stats
            stats = await get_network_stats(pool, uid)
            return _json_resp(stats)
        except Exception as e:
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/network/templates", network_templates)
    app.router.add_post("/api/miniapp/network/template", network_template_create)
    app.router.add_delete("/api/miniapp/network/template/{template_id}", network_template_delete)
    app.router.add_get("/api/miniapp/network/instances", network_instances)
    app.router.add_post("/api/miniapp/network/instance", network_instance_create)
    app.router.add_get("/api/miniapp/network/instance/{instance_id}", network_instance_detail)
    app.router.add_get("/api/miniapp/network/stats", network_stats)

    # ── Automation Workflows ─────────────────────────────────────────────────

    async def workflow_list(request: web.Request) -> web.Response:
        """Список воркфлоу в форме экрана (раньше звал несуществующий
        list_workflows → 500). Прямой запрос: статус из is_active, число шагов из
        inline-jsonb, last_run из workflow_runs."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await _safe_fetch(pool,
                """SELECT wd.id, wd.name,
                          CASE WHEN COALESCE(wd.is_active,FALSE) THEN 'active' ELSE 'paused' END AS status,
                          COALESCE(jsonb_array_length(wd.steps), 0) AS step_count,
                          (SELECT MAX(r.started_at) FROM workflow_runs r
                           WHERE r.workflow_id = wd.id) AS last_run
                   FROM workflow_definitions wd
                   WHERE wd.owner_id=$1 ORDER BY wd.name""", uid)
            workflows = [{
                "id": r["id"], "name": r["name"], "status": r["status"],
                "step_count": int(r["step_count"] or 0),
                "last_run": r["last_run"].isoformat() if r["last_run"] else None,
            } for r in (rows or [])]
            return _json_resp({"workflows": workflows})
        except Exception as exc:
            log.exception("workflow_list uid=%d", uid)
            return _err(str(exc), 500)

    async def workflow_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON", 400)
        name = (body.get("name") or "").strip()
        steps = body.get("steps")
        if not name:
            return _err("name обязателен", 400)
        if not isinstance(steps, list) or not steps:
            return _err("steps обязателен и должен быть списком", 400)
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                return _err(f"Шаг {i+1} должен быть объектом", 400)
            if not step.get("action"):
                return _err(f"Шаг {i+1}: action обязателен", 400)
        try:
            from services.workflow_engine import create_workflow
            wf_id = await create_workflow(pool, uid, name, steps)
            return _json_resp({"ok": True, "workflow_id": wf_id})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as exc:
            log.exception("workflow_create uid=%d", uid)
            return _err(str(exc), 500)

    async def workflow_execute(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wf_id = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad wf_id", 400)
        try:
            from services.workflow_engine import execute_workflow
            result = await execute_workflow(pool, uid, wf_id)
            return _json_resp({"ok": True, **result})
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as exc:
            log.exception("workflow_execute uid=%d wf=%d", uid, wf_id)
            return _err(str(exc), 500)

    async def workflow_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wf_id = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad wf_id", 400)
        try:
            from services.workflow_engine import get_workflow_status
            status = await get_workflow_status(pool, uid, wf_id)
            return _json_resp(status)
        except LookupError as e:
            return _err(str(e), 404)
        except Exception as exc:
            log.exception("workflow_status uid=%d wf=%d", uid, wf_id)
            return _err(str(exc), 500)

    async def workflow_pause(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wf_id = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad wf_id", 400)
        try:
            from services.workflow_engine import pause_workflow
            result = await pause_workflow(pool, uid, wf_id)
            return _json_resp({"ok": True, **result})
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as exc:
            log.exception("workflow_pause uid=%d wf=%d", uid, wf_id)
            return _err(str(exc), 500)

    async def workflow_resume(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wf_id = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad wf_id", 400)
        try:
            from services.workflow_engine import resume_workflow
            result = await resume_workflow(pool, uid, wf_id)
            return _json_resp({"ok": True, **result})
        except LookupError as e:
            return _err(str(e), 404)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception as exc:
            log.exception("workflow_resume uid=%d wf=%d", uid, wf_id)
            return _err(str(exc), 500)

    async def workflow_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            wf_id = int(request.match_info["wf_id"])
        except (KeyError, ValueError):
            return _err("bad wf_id", 400)
        try:
            from services.workflow_engine import delete_workflow
            ok = await delete_workflow(pool, uid, wf_id)
            if not ok:
                return _err("Workflow не найден", 404)
            return _json_resp({"ok": True})
        except Exception as exc:
            log.exception("workflow_delete uid=%d wf=%d", uid, wf_id)
            return _err(str(exc), 500)

    app.router.add_get("/api/miniapp/workflows", workflow_list)
    app.router.add_post("/api/miniapp/workflows", workflow_create_plural)
    app.router.add_get("/api/miniapp/workflows/{wf_id}", workflow_detail_plural)
    app.router.add_patch("/api/miniapp/workflows/{wf_id}", workflow_toggle_plural)
    app.router.add_delete("/api/miniapp/workflows/{wf_id}", workflow_delete_plural)
    app.router.add_post("/api/miniapp/workflows/{wf_id}/steps", workflow_add_step)
    app.router.add_post("/api/miniapp/workflow/create", workflow_create)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/execute", workflow_execute)
    app.router.add_get("/api/miniapp/workflow/{wf_id}/status", workflow_status)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/pause", workflow_pause)
    app.router.add_post("/api/miniapp/workflow/{wf_id}/resume", workflow_resume)
    app.router.add_delete("/api/miniapp/workflow/{wf_id}", workflow_delete)

    # ── Audience Analytics ─────────────────────────────────────────────────

    async def audience_analyze(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            channel_id = int(request.match_info["channel_id"])
        except (KeyError, ValueError):
            return _err("bad channel_id", 400)
        try:
            from services.audience_analytics import analyze_audience
            overview = await analyze_audience(pool, uid, channel_id)
            if not overview:
                return _err("Нет данных для анализа", 404)
            return _json_resp({
                "total_subscribers": overview.total_subscribers,
                "active_users": overview.active_users,
                "active_rate": overview.active_rate,
                "peak_hour": overview.peak_hour,
                "peak_day": overview.peak_day,
                "retention_7d": overview.retention_7d,
                "retention_30d": overview.retention_30d,
                "avg_session_duration_min": overview.avg_session_duration_min,
            })
        except Exception as exc:
            log.exception("audience_analyze uid=%d ch=%d", uid, channel_id)
            return _err(str(exc), 500)

    async def audience_segment(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            channel_id = int(request.match_info["channel_id"])
        except (KeyError, ValueError):
            return _err("bad channel_id", 400)
        try:
            from services.audience_analytics import segment_audience
            segments = await segment_audience(pool, uid, channel_id)
            return _json_resp({"segments": [
                {
                    "name": s.name,
                    "user_count": s.user_count,
                    "percentage": s.percentage,
                    "avg_engagement": s.avg_engagement,
                    "description": s.description,
                    "tags": s.tags,
                }
                for s in segments
            ]})
        except Exception as exc:
            log.exception("audience_segment uid=%d ch=%d", uid, channel_id)
            return _err(str(exc), 500)

    app.router.add_get("/api/miniapp/audience/analyze/{channel_id}", audience_analyze)
    app.router.add_get("/api/miniapp/audience/segment/{channel_id}", audience_segment)

    # ── Analytics Dashboard ───────────────────────────────────────────────

    async def analytics_dashboard(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.analytics_dashboard import get_dashboard_stats
            stats = await get_dashboard_stats(pool, uid)
            return _json_resp(stats)
        except Exception as exc:
            log.exception("analytics_dashboard uid=%d", uid)
            return _err(str(exc), 500)

    async def analytics_realtime(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        try:
            from services.analytics_dashboard import get_realtime_metrics
            metrics = await get_realtime_metrics(pool, uid)
            return _json_resp(metrics)
        except Exception as exc:
            log.exception("analytics_realtime uid=%d", uid)
            return _err(str(exc), 500)

    async def analytics_historical(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid: return _err("Unauthorized", 401)
        metric = request.query.get("metric", "operations")
        try:
            days = max(1, min(365, int(request.query.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        try:
            from services.analytics_dashboard import get_historical_data
            data = await get_historical_data(pool, uid, metric, days)
            return _json_resp({"metric": metric, "days": days, "data": data})
        except Exception as exc:
            log.exception("analytics_historical uid=%d", uid)
            return _err(str(exc), 500)

    app.router.add_get("/api/miniapp/analytics/dashboard", analytics_dashboard)
    app.router.add_get("/api/miniapp/analytics/realtime", analytics_realtime)
    app.router.add_get("/api/miniapp/analytics/historical", analytics_historical)

    # SSE
    app.router.add_get("/api/miniapp/events", events)

    # ── Diagnostics ──────────────────────────────────────────────────────────
    async def api_health(request: web.Request) -> web.Response:
        """Публичный health endpoint — только статус БД, без sensitive данных."""
        checks = {}
        try:
            await pool.fetchval("SELECT 1")
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {e}"
        checks["status"] = "ok" if checks.get("db") == "ok" else "degraded"
        return _json_resp(checks)
    app.router.add_get("/api/miniapp/sys_health", api_health)

    # ── Admin endpoints (только для админов) ────────────────────────────────

    async def admin_users(request: web.Request) -> web.Response:
        """Список пользователей платформы (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            rows = await pool.fetch(
                """SELECT user_id, username, first_name, current_plan, plan_expires_at,
                          registered_at AS created_at, last_seen AS last_active_at
                   FROM platform_users ORDER BY registered_at DESC LIMIT 100""")
            return _json_resp({"users": [dict(r) for r in rows]})
        except Exception as e:
            log.exception("admin_users uid=%d", uid)
            return _err(str(e), 500)

    async def admin_stats(request: web.Request) -> web.Response:
        """Системная статистика (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            stats = {}
            stats["total_users"] = int(await pool.fetchval("SELECT COUNT(*) FROM platform_users") or 0)
            stats["active_subs"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE is_active=true AND expires_at > now()") or 0)
            stats["total_bots"] = int(await pool.fetchval("SELECT COUNT(*) FROM managed_bots WHERE is_active=true") or 0)
            stats["total_channels"] = int(await pool.fetchval("SELECT COUNT(*) FROM managed_channels") or 0)
            stats["total_accounts"] = int(await pool.fetchval("SELECT COUNT(*) FROM tg_accounts WHERE is_active=true") or 0)
            stats["ops_today"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE created_at > now() - INTERVAL '1 day'") or 0)
            stats["ops_pending"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='pending'") or 0)
            stats["ops_running"] = int(await pool.fetchval(
                "SELECT COUNT(*) FROM operation_queue WHERE status='running'") or 0)
            return _json_resp(stats)
        except Exception as e:
            log.exception("admin_stats uid=%d", uid)
            return _err(str(e), 500)

    async def admin_user_detail(request: web.Request) -> web.Response:
        """Детали пользователя (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            target_id = int(request.match_info["user_id"])
        except (KeyError, ValueError):
            return _err("bad user_id", 400)
        try:
            user = await pool.fetchrow(
                """SELECT *, registered_at AS created_at, last_seen AS last_active_at
                   FROM platform_users WHERE user_id=$1""", target_id)
            if not user:
                return _err("User not found", 404)
            bots = await pool.fetchval(
                "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=true", target_id) or 0
            channels = await pool.fetchval(
                "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", target_id) or 0
            accounts = await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=true", target_id) or 0
            return _json_resp({
                "user": dict(user),
                "bots": int(bots),
                "channels": int(channels),
                "accounts": int(accounts),
            })
        except Exception as e:
            log.exception("admin_user_detail uid=%d target=%d", uid, target_id)
            return _err(str(e), 500)

    async def admin_broadcast(request: web.Request) -> web.Response:
        """Рассылка всем пользователям (только для админов)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            body = await request.json()
        except Exception:
            return _err("Invalid JSON")
        text = (body.get("text") or "").strip()
        if not text:
            return _err("text required")
        try:
            # Не шлём забаненным.
            users = await pool.fetch(
                "SELECT user_id FROM platform_users WHERE COALESCE(is_banned, false) = false"
            )
            if not users:
                return _json_resp({"ok": True, "sent": 0, "failed": 0})
            from aiogram import Bot as _Bot
            from aiogram.client.default import DefaultBotProperties
            from aiogram.enums import ParseMode
            from config import BOT_TOKEN as _tok
            # ОДИН Bot на всю рассылку (раньше создавался на каждого юзера —
            # утечка aiohttp-сессий). Закрываем сессию в finally.
            _b = _Bot(token=_tok, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            sent = 0
            failed = 0
            try:
                for u in users:
                    try:
                        await _b.send_message(u["user_id"], text)
                        sent += 1
                    except Exception:
                        failed += 1
                    await asyncio.sleep(0.05)
            finally:
                try:
                    await _b.session.close()
                except Exception as e:
                    log.warning("admin_broadcast session.close: %s", e)
            return _json_resp({"ok": True, "sent": sent, "failed": failed})
        except Exception as e:
            log.exception("admin_broadcast uid=%d", uid)
            return _err(str(e), 500)

    async def _admin_target(request: web.Request):
        """Общая проверка админ-действия: (uid, target_id) или (None, error-resp)."""
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return None, None, _err("Forbidden", 403)
        try:
            target_id = int(request.match_info["user_id"])
        except (KeyError, ValueError):
            return None, None, _err("bad user_id", 400)
        return uid, target_id, None

    async def admin_user_grant(request: web.Request) -> web.Response:
        """Выдать подписку пользователю (paid) на N месяцев."""
        uid, target_id, err = await _admin_target(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            months = int(body.get("months") or 1)
        except (TypeError, ValueError):
            months = 1
        months = max(1, min(months, 120))
        try:
            from database import db as _db
            await _db.grant_plan_to_user(pool, target_id, uid, "paid", months)
            return _json_resp({"ok": True, "months": months})
        except Exception as e:
            log.exception("admin_user_grant uid=%d target=%d", uid, target_id)
            return _err(str(e), 500)

    async def admin_user_revoke(request: web.Request) -> web.Response:
        """Отозвать подписку пользователя."""
        uid, target_id, err = await _admin_target(request)
        if err:
            return err
        try:
            from database import db as _db
            await _db.revoke_plan_from_user(pool, target_id, uid)
            return _json_resp({"ok": True})
        except Exception as e:
            log.exception("admin_user_revoke uid=%d target=%d", uid, target_id)
            return _err(str(e), 500)

    async def admin_user_ban(request: web.Request) -> web.Response:
        """Забанить пользователя (нельзя банить админов и себя)."""
        uid, target_id, err = await _admin_target(request)
        if err:
            return err
        if target_id == uid or _is_admin(target_id):
            return _err("Нельзя забанить администратора", 400)
        try:
            from database import db as _db
            await _db.ban_user(pool, target_id, uid, "Забанен из mini app")
            return _json_resp({"ok": True})
        except Exception as e:
            log.exception("admin_user_ban uid=%d target=%d", uid, target_id)
            return _err(str(e), 500)

    async def admin_user_unban(request: web.Request) -> web.Response:
        """Разбанить пользователя."""
        uid, target_id, err = await _admin_target(request)
        if err:
            return err
        try:
            from database import db as _db
            await _db.unban_user(pool, target_id, uid)
            return _json_resp({"ok": True})
        except Exception as e:
            log.exception("admin_user_unban uid=%d target=%d", uid, target_id)
            return _err(str(e), 500)

    async def admin_audit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            rows = await pool.fetch(
                """SELECT ol.op_id, ol.step_num, ol.target, ol.status, ol.message, ol.created_at,
                          oq.owner_id, oq.op_type, oq.label
                   FROM operation_log ol
                   JOIN operation_queue oq ON oq.id = ol.op_id
                   WHERE ol.created_at > NOW() - INTERVAL '24 hours'
                   ORDER BY ol.created_at DESC LIMIT 100""")
            return _json_resp({"audit": [
                {**dict(r), "created_at": r["created_at"].isoformat() if r["created_at"] else None}
                for r in rows
            ]})
        except Exception as e:
            log.exception("admin_audit uid=%d", uid)
            return _err(str(e), 500)

    async def admin_ops_stats(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid or not _is_admin(uid):
            return _err("Forbidden", 403)
        try:
            rows = await pool.fetch(
                """SELECT op_type, COUNT(*) as total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                          AVG(EXTRACT(EPOCH FROM (finished_at - started_at))) as avg_duration_s
                   FROM operation_queue
                   WHERE created_at > NOW() - INTERVAL '7 days'
                   GROUP BY op_type
                   ORDER BY total DESC""")
            return _json_resp({"stats": [
                {k: (round(float(v), 2) if k == "avg_duration_s" else int(v) if v is not None else 0) for k, v in dict(r).items()}
                for r in rows
            ]})
        except Exception as e:
            log.exception("admin_ops_stats uid=%d", uid)
            return _err(str(e), 500)

    app.router.add_get("/api/miniapp/admin/users", admin_users)
    app.router.add_get("/api/miniapp/admin/stats", admin_stats)
    app.router.add_get("/api/miniapp/admin/user/{user_id}", admin_user_detail)
    app.router.add_post("/api/miniapp/admin/broadcast", admin_broadcast)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/grant", admin_user_grant)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/revoke", admin_user_revoke)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/ban", admin_user_ban)
    app.router.add_post("/api/miniapp/admin/user/{user_id}/unban", admin_user_unban)
    app.router.add_get("/api/miniapp/admin/audit", admin_audit)
    app.router.add_get("/api/miniapp/admin/ops/stats", admin_ops_stats)

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
