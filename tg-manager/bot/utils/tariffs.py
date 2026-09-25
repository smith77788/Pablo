"""Single source of truth for the free/paid tariff system.

Every tariff value — plan levels, resource limits, monthly operation quotas,
feature→plan gating and prices — is defined here and is overridable from the
environment. No other module should hardcode a limit, a quota, a price or a
feature gate: they all derive from this module (see ``bot/utils/subscription.py``
and the payment services).

Design rules:
- Two tiers only: ``free`` (minimum) and ``paid`` (maximum).
- "Unlimited" is the named constant :data:`UNLIMITED` (a large int, safe for
  ``>=`` comparisons and JSON) — never a bare magic number like ``9999``.
- Every default has an env override so numbers can change without a deploy:
    * resource limits  → ``LIMIT_<PLAN>_<RESOURCE>``   e.g. ``LIMIT_FREE_BOTS=5``
    * operation quotas → ``QUOTA_<PLAN>_<OP>``          e.g. ``QUOTA_FREE_DM_CAMPAIGN=5``
    * feature gating   → ``FEATURE_PLAN_<KEY>``         e.g. ``FEATURE_PLAN_CRM=free``
    * prices           → ``PRICE_<PLAN>`` (via config.PLAN_PRICES_USD)
  An env value of ``unlimited``/``inf``/``-1`` (case-insensitive) means UNLIMITED.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Large enough to be "unlimited" for any real count, small enough to stay a
# normal int (JSON-safe, unlike math.inf). Comparisons like ``count >= UNLIMITED``
# are always False in practice, so a paid user is never blocked.
UNLIMITED: int = 1_000_000_000

PLANS: tuple[str, ...] = ("free", "paid")
PLAN_LEVELS: dict[str, int] = {"free": 0, "paid": 1}

# Legacy/marketing plan names that map onto the two canonical tiers.
PLAN_ALIASES: dict[str, str] = {
    "max": "paid",
    "maximum": "paid",
    "starter": "paid",
    "pro": "paid",
    "enterprise": "paid",
}

_UNLIMITED_WORDS = {"unlimited", "inf", "infinity", "∞", "-1", "none"}


# ── env parsing ──────────────────────────────────────────────────────────────

def _limit_env(name: str, default: int) -> int:
    """Read an integer limit from env; blank/unset → default, word → UNLIMITED."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in _UNLIMITED_WORDS:
        return UNLIMITED
    try:
        parsed = int(val)
    except ValueError:
        log.warning("tariffs: bad value for %s=%r — using default %s", name, raw, default)
        return default
    return UNLIMITED if parsed < 0 else parsed


def _env_key(*parts: str) -> str:
    return "_".join(p.upper() for p in parts)


# ── resource limits (per-plan caps on owned resources) ───────────────────────

# default[resource][plan]
_RESOURCE_DEFAULTS: dict[str, dict[str, int]] = {
    "bots": {"free": 5, "paid": UNLIMITED},
    "channels": {"free": 5, "paid": UNLIMITED},
    # Telegram-аккаунты (tg_accounts) — платная возможность.
    "accounts": {"free": 0, "paid": UNLIMITED},
    # Ключевые слова трекера позиций (ranking) — платная возможность.
    "ranking_keywords": {"free": 0, "paid": UNLIMITED},
    # Правила авто-ответов на бота — free получает базовые 5.
    "auto_reply_rules": {"free": 5, "paid": UNLIMITED},
}


def resource_limit(resource: str, plan: str) -> int:
    """Cap on a resource (e.g. 'bots', 'channels') for a plan. Env-overridable."""
    plan = normalize_plan(plan)
    defaults = _RESOURCE_DEFAULTS.get(resource, {})
    default = defaults.get(plan, defaults.get("free", 0))
    return _limit_env(_env_key("LIMIT", plan, resource), default)


def resource_limits(resource: str) -> dict[str, int]:
    """{plan: limit} for a resource — for callers that want the whole map."""
    return {plan: resource_limit(resource, plan) for plan in PLANS}


# Операции гейтятся строго по тарифу (OP_REGISTRY[op]['min_plan'], enforced в
# operation_bus.submit()): на free — недоступны, на paid — без ограничений.
# Отдельной месячной квоты операций нет намеренно (доступ бинарный по плану) —
# так конфиг не расходится с фактическим поведением.


# ── feature → required plan ──────────────────────────────────────────────────

_FEATURE_DEFAULTS: dict[str, str] = {
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


def feature_plan(feature_key: str) -> str:
    """Plan required for a feature. Env override: FEATURE_PLAN_<KEY>.

    Unknown features default to ``paid`` (fail-closed: never expose a new
    feature to free users by accident).
    """
    override = os.getenv(_env_key("FEATURE_PLAN", feature_key))
    if override:
        return normalize_plan(override)
    return _FEATURE_DEFAULTS.get(feature_key, "paid")


def feature_keys() -> tuple[str, ...]:
    return tuple(_FEATURE_DEFAULTS)


def feature_plan_map() -> dict[str, str]:
    return {key: feature_plan(key) for key in _FEATURE_DEFAULTS}


# ── Что тариф даёт: русские подписи и сводная матрица ────────────────────────
# Экран тарифов в мини-аппе был написан руками и разошёлся с тем, что реально
# гейтится: бесплатному обещали «50 аккаунтов» при фактическом нуле и «базовые
# рассылки», которые на деле платные, а платному — «500 аккаунтов» вместо
# безлимита. Поэтому экран собирается ИЗ ЭТИХ ЖЕ значений, по которым проверка
# доступа и работает: разойтись им теперь негде.

PLAN_LABEL_RU: dict[str, str] = {"free": "Бесплатный", "paid": "Платный"}

RESOURCE_LABEL_RU: dict[str, str] = {
    "bots": "Управляемых ботов",
    "channels": "Каналов",
    "accounts": "Telegram-аккаунтов",
    "ranking_keywords": "Ключевых слов трекера позиций",
    "auto_reply_rules": "Правил авто-ответов",
}

# Подписи возможностей для витрины. Показываем не все ключи подряд, а те, что
# покупателю что-то говорят; порядок — как на экране.
FEATURE_LABEL_RU: dict[str, str] = {
    "basic_bots": "Управление ботами",
    "basic_broadcast": "Рассылки",
    "inbox": "Общий входящий",
    "account_ops": "Операции с аккаунтами",
    "bulk_operations": "Массовые операции",
    "audience_parser": "Парсер аудитории",
    "channel_factory": "Фабрика каналов",
    "funnels": "Авто-воронки",
    "crm": "CRM и сделки",
    "seo": "SEO и трекер позиций",
    "proxy_manager": "Менеджер прокси",
    "ai_assistant": "ИИ-помощник",
    "autonomous_engine": "Автономный движок",
    "strike": "Strike",
    "workspaces": "Совместная работа",
}


def plan_matrix() -> list[dict]:
    """Что даёт каждый тариф — из тех же значений, по которым гейтится доступ.

    Единственный источник правды для витрины: лимиты берутся из
    `resource_limit` (с учётом env-переопределений), возможности — из
    `feature_plan`. Руками на экране ничего не пишется, поэтому расходиться с
    фактическим поведением нечему.
    """
    out: list[dict] = []
    for plan in PLANS:
        limits = [
            {"key": key, "label": RESOURCE_LABEL_RU.get(key, key),
             "value": resource_limit(key, plan),
             "display": format_limit(resource_limit(key, plan))}
            for key in _RESOURCE_DEFAULTS
        ]
        features = [
            {"key": key, "label": label,
             "included": PLAN_LEVELS.get(plan, 0) >= PLAN_LEVELS.get(
                 feature_plan(key), 0)}
            for key, label in FEATURE_LABEL_RU.items()
        ]
        out.append({
            "plan": plan,
            "label": PLAN_LABEL_RU.get(plan, plan),
            "price_usd": price_usd(plan),
            "limits": limits,
            "features": features,
        })
    return out


# ── plan normalization ───────────────────────────────────────────────────────

def normalize_plan(plan: str | None) -> str:
    normalized = (plan or "free").lower()
    return PLAN_ALIASES.get(normalized, normalized)


def coerce_plan(plan: str | None) -> str:
    """Normalize + validate; unknown plans fail safe to 'free'."""
    normalized = normalize_plan(plan)
    if normalized in PLAN_LEVELS:
        return normalized
    log.warning("unknown subscription plan %r coerced to free", plan)
    return "free"


# ── prices ───────────────────────────────────────────────────────────────────

def price_usd(plan: str) -> int:
    """Plan price in USD, sourced from config.PLAN_PRICES_USD (env PRICE_<PLAN>).

    Never hardcoded here; free is always 0.
    """
    plan = coerce_plan(plan)
    if plan == "free":
        return 0
    try:
        from config import PLAN_PRICES_USD

        return int(PLAN_PRICES_USD.get(plan, PLAN_PRICES_USD.get("paid", 0)))
    except Exception:
        log.warning("tariffs: cannot read PLAN_PRICES_USD for %r", plan, exc_info=True)
        return 0


def price_str(plan: str) -> str:
    """Human price like ``$29`` (or ``Free`` for the free tier)."""
    plan = coerce_plan(plan)
    if plan == "free":
        return "Free"
    return f"${price_usd(plan)}"


# ── display helpers ──────────────────────────────────────────────────────────

def is_unlimited(value: int) -> bool:
    return value >= UNLIMITED


def format_limit(value: int) -> str:
    """Render a limit for the UI: unlimited → ∞, else the number."""
    return "∞" if is_unlimited(value) else str(value)
