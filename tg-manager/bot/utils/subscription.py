from __future__ import annotations
import logging
import os
import asyncpg

PLAN_LEVELS: dict[str, int] = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
BOT_LIMITS: dict[str, int] = {"free": 3, "starter": 10, "pro": 30, "enterprise": 9999}
PLAN_PRICES = {"starter": "$9", "pro": "$25", "enterprise": "$69"}
PLAN_EMOJIS = {"free": "🆓", "starter": "⭐", "pro": "🚀", "enterprise": "👑"}
PLAN_FEATURES = {
    "starter": "10 ботов, CRM, воронки, диплинки, SEO, трекер позиций, 1 аккаунт",
    "pro": "30 ботов, Фабрики каналов/групп, парсер, A/B тесты, мультигео, 3 аккаунта",
    "enterprise": "∞ ботов+аккаунтов, AI-ассистент, DM-кампании, Global Presence, сеть ботов, Swarm",
}

from services.logger import log_exc_swallow  # noqa: E402

log = logging.getLogger(__name__)


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
        pass
    return False


async def get_plan(pool: asyncpg.Pool, user_id: int) -> str:
    if is_platform_admin(user_id):
        return "enterprise"
    row = await pool.fetchrow(
        "SELECT plan FROM subscriptions "
        "WHERE user_id=$1 AND is_active=true AND expires_at > now()",
        user_id,
    )
    return row["plan"] if row else "free"


async def require_plan(pool: asyncpg.Pool, user_id: int, min_plan: str) -> bool:
    if is_platform_admin(user_id):
        return True
    plan = await get_plan(pool, user_id)
    return PLAN_LEVELS.get(plan, 0) >= PLAN_LEVELS.get(min_plan, 0)


async def get_bot_limit(pool: asyncpg.Pool, user_id: int) -> int:
    if is_platform_admin(user_id):
        return 9999
    plan = await get_plan(pool, user_id)
    return BOT_LIMITS.get(plan, 3)


def locked_text(feature: str, required_plan: str) -> str:
    emoji = PLAN_EMOJIS.get(required_plan, "🔒")
    price = PLAN_PRICES.get(required_plan, "")
    features = PLAN_FEATURES.get(required_plan, "")
    upsell = ""
    if required_plan == "enterprise":
        upsell = "\n\n💡 <b>Enterprise — лучший выбор:</b> все функции без исключения, без лимитов на ботов и аккаунты."
    elif required_plan == "pro":
        upsell = "\n\n💡 <i>PRO: 30 ботов, Фабрики каналов/групп, A/B тесты, мультигео, парсер аудитории, 3 аккаунта</i>"
    elif required_plan == "starter":
        upsell = "\n\n💡 <i>STARTER: за $9/мес — CRM, воронки, диплинки, SEO, трекер позиций и многое другое</i>"
    return (
        f"🔒 <b>{feature}</b>\n\n"
        f"Эта функция доступна с планом {emoji} <b>{required_plan.upper()}</b>.\n\n"
        f"{emoji} {required_plan.upper()} — {price}/мес\n"
        f"<i>{features}</i>"
        f"{upsell}\n\n"
        f"Оформить подписку: /subscription"
    )
