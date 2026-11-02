"""Три тарифа подписки по объёму флота: Старт, Про, Бизнес.

ЗАЧЕМ. Подписка была одна — 29 $ за всё без ограничений. Владельцу с тремя
аккаунтами она дорога, агентству с сотней — дёшева; у лидеров цена растёт с
объёмом. Решение владельца (ноябрь 2026): возможности у всех тарифов одинаковые,
различается только размер флота.

    Старт    15 $/мес   до 10 аккаунтов
    Про      29 $/мес   до 50 аккаунтов
    Бизнес   79 $/мес   без лимита

Уровень доступа к функциям по-прежнему `paid` (bot/utils/tariffs.py); тариф
хранится рядом — `subscriptions.tier` и `payments.tier` (schema_v272).
`tier IS NULL` у действующей подписки — подписка, купленная до тарифов,
пробный период, награда или ручная выдача: лимита флота у неё нет (прежние
условия сохраняются до конца срока).

Цены и лимиты меняются из окружения без деплоя: `PRICE_TIER_<T>`,
`LIMIT_TIER_<T>_ACCOUNTS` (unlimited/-1 — без лимита).
"""
from __future__ import annotations

import logging
import os

from bot.utils import tariffs

log = logging.getLogger(__name__)

TIERS: tuple[str, ...] = ("start", "pro", "business")
DEFAULT_TIER = "pro"

_NAMES = {"start": "Старт", "pro": "Про", "business": "Бизнес"}
_EMOJI = {"start": "🚀", "pro": "💎", "business": "🏢"}
_PRICE_DEFAULTS = {"start": 15, "pro": 29, "business": 79}
_ACCOUNT_DEFAULTS = {"start": 10, "pro": 50, "business": tariffs.UNLIMITED}


def coerce_tier(tier: str | None) -> str | None:
    t = (tier or "").strip().lower()
    return t if t in TIERS else None


def name(tier: str | None) -> str:
    return _NAMES.get(tier or "", "Подписка")


def emoji(tier: str | None) -> str:
    return _EMOJI.get(tier or "", "💎")


def price_usd(tier: str) -> int:
    default = _PRICE_DEFAULTS[tier]
    raw = (os.getenv(f"PRICE_TIER_{tier.upper()}") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("tiers: bad PRICE_TIER_%s=%r", tier.upper(), raw)
        return default
    return val if val > 0 else default


def account_cap(tier: str | None) -> int:
    """Лимит аккаунтов тарифа. Неизвестный/NULL — без лимита (см. докстринг)."""
    t = coerce_tier(tier)
    if t is None:
        return tariffs.UNLIMITED
    return tariffs._limit_env(f"LIMIT_TIER_{t.upper()}_ACCOUNTS", _ACCOUNT_DEFAULTS[t])


def cap_label(tier: str | None) -> str:
    cap = account_cap(tier)
    return "без лимита аккаунтов" if cap >= tariffs.UNLIMITED else f"до {cap} аккаунтов"


def for_fleet(count: int) -> str:
    """Самый дешёвый тариф, вмещающий флот `count`. ЧИСТАЯ."""
    for t in TIERS:
        if count <= account_cap(t):
            return t
    return TIERS[-1]


async def active_tier(pool, owner_id: int) -> tuple[bool, str | None]:
    """(есть ли действующая подписка, её тариф или None)."""
    row = await pool.fetchrow(
        "SELECT tier FROM subscriptions WHERE user_id=$1 AND is_active "
        "AND expires_at > now()", owner_id)
    if row:
        return True, coerce_tier(row["tier"])
    return False, None


async def owner_account_cap(pool, owner_id: int) -> int:
    """Сколько аккаунтов владелец может держать прямо сейчас.

    Подписка с тарифом — лимит тарифа; подписка без тарифа, админ, ручная
    выдача — без лимита; бесплатный тариф — `LIMIT_FREE_ACCOUNTS` (0), но 1,
    если ещё положен пробный период: он выдаётся после первого аккаунта.
    """
    from bot.utils import subscription as sub

    if sub.is_platform_admin(owner_id):
        return tariffs.UNLIMITED
    has_sub, tier = await active_tier(pool, owner_id)
    if has_sub:
        return account_cap(tier)
    if await sub.get_plan(pool, owner_id) != "free":
        return tariffs.UNLIMITED  # ручная выдача в platform_users
    cap = tariffs.resource_limit("accounts", "free")
    if cap == 0:
        from services import trial
        if await trial.is_eligible(pool, owner_id, need_account=False):
            return 1
    return cap
