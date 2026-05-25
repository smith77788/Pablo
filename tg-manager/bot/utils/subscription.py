from __future__ import annotations
import asyncpg

PLAN_LEVELS: dict[str, int] = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}
BOT_LIMITS: dict[str, int] = {"free": 3, "starter": 10, "pro": 30, "enterprise": 9999}
PLAN_PRICES = {"starter": "$9", "pro": "$25", "enterprise": "$69"}
PLAN_EMOJIS = {"free": "🆓", "starter": "⭐", "pro": "🚀", "enterprise": "👑"}
PLAN_FEATURES = {
    "starter": "Inbox, CRM, автоматизация, цепочки, расписание, диплинки, SEO",
    "pro": "A/B тесты, активность, мультигео, массовые операции, аналитика сети",
    "enterprise": "Swarm, роутинг, кластеры, сетевая рассылка, клонирование, AI-ассистент",
}


async def get_plan(pool: asyncpg.Pool, user_id: int) -> str:
    row = await pool.fetchrow(
        "SELECT plan FROM subscriptions "
        "WHERE user_id=$1 AND is_active=true AND expires_at > now()",
        user_id,
    )
    return row["plan"] if row else "free"


async def require_plan(pool: asyncpg.Pool, user_id: int, min_plan: str) -> bool:
    plan = await get_plan(pool, user_id)
    return PLAN_LEVELS.get(plan, 0) >= PLAN_LEVELS.get(min_plan, 0)


async def get_bot_limit(pool: asyncpg.Pool, user_id: int) -> int:
    plan = await get_plan(pool, user_id)
    return BOT_LIMITS.get(plan, 3)


def locked_text(feature: str, required_plan: str) -> str:
    emoji = PLAN_EMOJIS.get(required_plan, "🔒")
    price = PLAN_PRICES.get(required_plan, "")
    features = PLAN_FEATURES.get(required_plan, "")
    return (
        f"🔒 <b>{feature} — {required_plan.upper()}</b>\n\n"
        f"Эта функция доступна с подпиской <b>{required_plan.upper()}</b>.\n\n"
        f"{emoji} {required_plan.upper()} — {price}/мес\n"
        f"<i>{features}</i>\n\n"
        f"Оформить: /subscription"
    )
