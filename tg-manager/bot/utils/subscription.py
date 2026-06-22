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
BOT_LIMITS: dict[str, int] = {"free": 5, "paid": 9999}
CHANNEL_LIMITS: dict[str, int] = {"free": 5, "paid": 9999}
PLAN_PRICES = {"paid": "$29"}
PLAN_EMOJIS = {"free": "🆓", "paid": "💎"}
PLAN_FEATURES = {
    "paid": "∞ ботов и каналов, CRM, воронки, аккаунты, AI-ассистент, рассылки, аналитика, все функции",
}

# Фича-специфичный upsell — показывается под замком каждой конкретной функции
_FEATURE_UPSELL: dict[str, str] = {
    "Публикация в каналы": (
        "💡 С подпиской за 30 секунд публикуешь одновременно во все свои каналы — "
        "без открытия каждого отдельно. Авто-расписание, медиа, задержки между постами."
    ),
    "Mass Publish": (
        "💡 Публикация во всю сеть каналов одной кнопкой. "
        "Текст + медиа + умные задержки чтобы не получить flood от Telegram."
    ),
    "DM-кампании": (
        "💡 Персональные сообщения тысячам пользователей напрямую. "
        "Конверсия DM в 3-5× выше чем посты. Spintax, сегментация, аналитика доставки."
    ),
    "CRM & автоматизация": (
        "💡 Теги, сегменты, воронки. Знай каждого подписчика: "
        "когда пришёл, что нажал, где отвалился. Автоматизируй продажи."
    ),
    "AI-ассистент": (
        "💡 Claude AI прямо в боте — анализ контента, генерация постов, "
        "ответы на вопросы по твоей аудитории. Безлимитные запросы."
    ),
    "Аналитика": (
        "💡 Поведенческая аналитика: вовлечённость, активность по часам, "
        "сравнение сегментов. Знай что работает, а что — нет."
    ),
    "Strike": (
        "💡 Автоматический мониторинг и защита каналов. "
        "Реакции, комментарии, выявление ботов — всё в одном инструменте."
    ),
    "Global Presence": (
        "💡 Массовое присутствие в Telegram: подписки, вступления, взаимодействия "
        "через сеть аккаунтов. Рост органической аудитории на автопилоте."
    ),
    "Сетевая рассылка": (
        "💡 Рассылай по аудиториям всех своих ботов одновременно. "
        "Сегментация по языку, активности, когортам. Отчёты в реальном времени."
    ),
    "Парсер аудитории": (
        "💡 Собирай аудиторию из любых каналов и групп Telegram. "
        "Фильтры по активности, дате вступления, наличию username."
    ),
    "Фабрика каналов": (
        "💡 Создавай и настраивай десятки каналов за минуты: "
        "названия, описания, ссылки, аватары — массово через один интерфейс."
    ),
    "Воронки": (
        "💡 Цепочки сообщений с задержками, условиями и A/B тестами. "
        "Автоматически веди пользователя от первого сообщения до покупки."
    ),
    "Inbox": (
        "💡 Единый входящий для всех твоих ботов — ни одно обращение не потеряется. "
        "Фильтрация, теги, переадресация операторам."
    ),
    "Прокси": (
        "💡 Управляй сотнями прокси из одного места. "
        "Автоматическая ротация, health-check, привязка к аккаунтам."
    ),
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


_TRIAL_DAYS = 7  # дней бесплатного триала для новых аккаунтов


async def is_trial_active(pool: asyncpg.Pool, user_id: int) -> bool:
    """Возвращает True если 7-дневный триал ещё не истёк."""
    if is_platform_admin(user_id):
        return False
    try:
        started = await pool.fetchval(
            "SELECT trial_started_at FROM platform_users WHERE user_id=$1", user_id
        )
        if started is None:
            return True  # нет записи = новый пользователь
        import datetime
        from datetime import timezone
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.datetime.now(timezone.utc) - started).total_seconds()
        return elapsed < _TRIAL_DAYS * 86400
    except Exception:
        return True  # при ошибке не блокируем


async def get_trial_days_left(pool: asyncpg.Pool, user_id: int) -> int:
    """Сколько дней осталось в триале (0 = истёк)."""
    try:
        started = await pool.fetchval(
            "SELECT trial_started_at FROM platform_users WHERE user_id=$1", user_id
        )
        if started is None:
            return _TRIAL_DAYS
        import datetime
        from datetime import timezone
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed_days = (datetime.datetime.now(timezone.utc) - started).total_seconds() / 86400
        remaining = _TRIAL_DAYS - elapsed_days
        return max(0, int(remaining))
    except Exception:
        return _TRIAL_DAYS


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
    if coerce_plan(plan) == "free" and not await is_trial_active(pool, user_id):
        return 0  # триал истёк — нельзя добавлять новых ботов
    return BOT_LIMITS[coerce_plan(plan)]


async def get_channel_limit(pool: asyncpg.Pool, user_id: int) -> int:
    if is_platform_admin(user_id):
        return 9999
    plan = await get_plan(pool, user_id)
    if coerce_plan(plan) == "free" and not await is_trial_active(pool, user_id):
        return 0  # триал истёк — нельзя добавлять новые каналы
    return CHANNEL_LIMITS[coerce_plan(plan)]


async def get_effective_bot_count(pool: asyncpg.Pool, user_id: int) -> int:
    """Кол-во ботов с учётом связанных аккаунтов (защита от multi-account abuse)."""
    if is_platform_admin(user_id):
        return 0
    plan = await get_plan(pool, user_id)
    if coerce_plan(plan) != "free":
        from database import db as _db
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1", user_id
        ) or 0
    from database import db as _db
    return await _db.count_bots_across_linked(pool, user_id)


async def get_effective_channel_count(pool: asyncpg.Pool, user_id: int) -> int:
    """Кол-во каналов с учётом связанных аккаунтов (защита от multi-account abuse)."""
    if is_platform_admin(user_id):
        return 0
    plan = await get_plan(pool, user_id)
    if coerce_plan(plan) != "free":
        return await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", user_id
        ) or 0
    from database import db as _db
    return await _db.count_channels_across_linked(pool, user_id)


def locked_text(feature: str, required_plan: str) -> str:
    required_plan = coerce_plan(required_plan)
    emoji = PLAN_EMOJIS.get(required_plan, "💎")
    price = PLAN_PRICES.get(required_plan, "$29")
    upsell = _FEATURE_UPSELL.get(feature, "")
    upsell_block = f"\n{upsell}\n" if upsell else "\n"
    return (
        f"🔒 <b>{feature}</b>\n\n"
        f"Эта функция доступна с подпиской {emoji} ({price}/мес).\n"
        f"{upsell_block}\n"
        f"👉 <b>Оформить подписку:</b> /subscription\n\n"
        f"<i>Уже {BOT_LIMITS['free']}+ ботов и {CHANNEL_LIMITS['free']}+ каналов? "
        f"Подписка снимет все ограничения.</i>"
    )


def locked_text_with_social_proof(feature: str, required_plan: str, active_subs: int = 0) -> str:
    """Locked screen с социальным доказательством (кол-во подписчиков)."""
    required_plan = coerce_plan(required_plan)
    emoji = PLAN_EMOJIS.get(required_plan, "💎")
    price = PLAN_PRICES.get(required_plan, "$29")
    upsell = _FEATURE_UPSELL.get(feature, "")
    upsell_block = f"\n{upsell}\n" if upsell else "\n"
    social = f"🔥 <b>{active_subs}</b> пользователей уже с подпиской\n\n" if active_subs > 1 else ""
    return (
        f"🔒 <b>{feature}</b>\n\n"
        f"Доступно с подпиской {emoji} ({price}/мес).\n"
        f"{upsell_block}\n"
        f"{social}"
        f"👉 <b>Оформить:</b> /subscription"
    )
