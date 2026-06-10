from __future__ import annotations
import logging
import os
import time
import asyncpg

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_FREE_MODE: bool = False

# ── Plan cache: TTL-based, invalidated on subscription changes ────────────────
_plan_cache: dict[int, tuple[str, float]] = {}
_PLAN_CACHE_TTL = 60.0  # seconds


def get_free_mode() -> bool:
    return _FREE_MODE


def _global_free_mode_allowed() -> bool:
    return os.getenv("ALLOW_GLOBAL_FREE_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def set_free_mode(enabled: bool) -> None:
    global _FREE_MODE
    _FREE_MODE = bool(enabled and _global_free_mode_allowed())
    if enabled and not _FREE_MODE:
        log.warning(
            "free_mode requested but ignored: set ALLOW_GLOBAL_FREE_MODE=true to enable it"
        )


def invalidate_plan_cache(user_id: int) -> None:
    """Call after subscription purchase/change to force fresh DB lookup."""
    _plan_cache.pop(user_id, None)


PLAN_LEVELS: dict[str, int] = {"free": 0, "paid": 1}
BOT_LIMITS: dict[str, int] = {"free": 1, "paid": 9999}
CHANNEL_LIMITS: dict[str, int] = {"free": 1, "paid": 9999}
PLAN_PRICES = {"paid": "$29"}
PLAN_EMOJIS = {"free": "🆓", "paid": "💎"}
PLAN_FEATURES = {
    "paid": "∞ ботов и каналов, CRM, воронки, аккаунты, AI-ассистент, рассылки, аналитика, все функции",
}

PLAN_ALIASES: dict[str, str] = {
    "max": "paid",
    "maximum": "paid",
    "starter": "paid",
    "pro": "paid",
    "enterprise": "paid",
}
FEATURE_PLAN: dict[str, str] = {
    "basic_bots": "free",
    "basic_broadcast": "paid",
    "inbox": "paid",
    "funnels": "paid",
    "crm": "paid",
    "seo": "paid",
    "account_ops": "paid",
    "channel_factory": "paid",
    "audience_parser": "paid",
    "bulk_operations": "paid",
    "proxy_manager": "paid",
    "ai_assistant": "paid",
    "autonomous_engine": "paid",
    "global_presence": "paid",
    "swarm": "paid",
    "workspaces": "paid",
    "strike": "paid",
    "email_oauth": "paid",
    "infra_intelligence": "paid",
    "account_readiness": "paid",
}


def normalize_plan(plan: str) -> str:
    normalized = (plan or "free").lower()
    return PLAN_ALIASES.get(normalized, normalized)


def coerce_plan(plan: str | None) -> str:
    normalized = normalize_plan(plan or "free")
    if normalized in PLAN_LEVELS:
        return normalized
    log.warning("unknown subscription plan %r coerced to free", plan)
    return "free"


def feature_required_plan(feature_key: str) -> str:
    return FEATURE_PLAN.get(feature_key, "paid")


def _admin_ids() -> set[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


def is_platform_admin(user_id: int) -> bool:
    """Admins bypass all subscription gates and get enterprise access."""
    ids = _admin_ids()
    if bool(ids) and user_id in ids:
        return True
    # Also check session admins set via ADMIN_SECRET in admin panel
    try:
        from bot.handlers.admin import _session_admins

        if user_id in _session_admins:
            return True
    except Exception:
        log_exc_swallow(log, "Ошибка проверки session-админов в is_platform_admin")
    return False


async def get_plan(pool: asyncpg.Pool, user_id: int) -> str:
    if is_platform_admin(user_id):
        return "paid"

    now = time.monotonic()
    cached = _plan_cache.get(user_id)
    if cached is not None:
        plan, ts = cached
        if now - ts < _PLAN_CACHE_TTL:
            return plan

    row = await pool.fetchrow(
        "SELECT plan FROM subscriptions "
        "WHERE user_id=$1 AND is_active=true AND expires_at > now()",
        user_id,
    )
    plan = coerce_plan(row["plan"] if row else "free")
    _plan_cache[user_id] = (plan, now)
    return plan


async def require_plan(pool: asyncpg.Pool, user_id: int, min_plan: str) -> bool:
    if _FREE_MODE:
        return True
    if is_platform_admin(user_id):
        return True
    plan = coerce_plan(await get_plan(pool, user_id))
    min_plan = coerce_plan(min_plan)
    return PLAN_LEVELS.get(plan, 0) >= PLAN_LEVELS.get(min_plan, 0)


async def require_feature(pool: asyncpg.Pool, user_id: int, feature_key: str) -> bool:
    return await require_plan(pool, user_id, feature_required_plan(feature_key))


async def get_bot_limit(pool: asyncpg.Pool, user_id: int) -> int:
    if is_platform_admin(user_id):
        return 9999
    plan = await get_plan(pool, user_id)
    return BOT_LIMITS[coerce_plan(plan)]


async def get_channel_limit(pool: asyncpg.Pool, user_id: int) -> int:
    if is_platform_admin(user_id):
        return 9999
    plan = await get_plan(pool, user_id)
    return CHANNEL_LIMITS[coerce_plan(plan)]


def locked_text(feature: str, required_plan: str) -> str:
    required_plan = coerce_plan(required_plan)
    emoji = PLAN_EMOJIS.get(required_plan, "💎")
    price = PLAN_PRICES.get(required_plan, "$29")
    features = PLAN_FEATURES.get(required_plan, "")
    return (
        f"🔒 <b>{feature}</b>\n\n"
        f"Эта функция доступна только с платной подпиской.\n\n"
        f"{emoji} <b>Подписка</b> — {price}/мес\n"
        f"<i>{features}</i>\n\n"
        f"Бесплатно: 1 бот и 1 канал для демо-проверки.\n"
        f"Все остальные функции — только с подпиской.\n\n"
        f"Оформить подписку: /subscription"
    )
