"""Canonical subscription activation.

Single source used by BOTH the payment webhook (`services/payment_webhook.py`)
and the on-chain payment checker (`services/payment_checker.py`), so the way a
subscription is granted/extended can never diverge between the two paths.

What it does (and nothing else — payment rows are recorded by each caller,
because their sources differ):
  1. Upsert `subscriptions` — extend from the current expiry if still active,
     otherwise start fresh. Returns the authoritative `expires_at`.
  2. Sync `platform_users.current_plan` + `plan_expires_at` to that SAME
     `expires_at` — parts of the code read platform_users directly, so the two
     tables must agree (previously the webhook synced only current_plan, the
     checker synced both from an independently-computed date → drift).
  3. Invalidate the in-process plan cache.

Accepts either a pool or an open connection as `executor`, so a caller can run
it inside an existing transaction (the webhook does).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bot.utils import tariffs

log = logging.getLogger(__name__)

_SUBSCRIPTION_UPSERT = """
    INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
    VALUES($1, $2, now() + ($3 || ' months')::INTERVAL, true)
    ON CONFLICT(user_id) DO UPDATE
    SET plan       = EXCLUDED.plan,
        is_active  = true,
        expires_at = CASE
            WHEN subscriptions.expires_at > now()
                THEN subscriptions.expires_at + ($3 || ' months')::INTERVAL
            ELSE now() + ($3 || ' months')::INTERVAL
        END,
        started_at = CASE
            WHEN subscriptions.expires_at > now() THEN subscriptions.started_at
            ELSE now()
        END
    RETURNING expires_at
"""

_PLATFORM_USERS_SYNC = """
    UPDATE platform_users SET current_plan=$2, plan_expires_at=$3 WHERE user_id=$1
"""


def is_subscription_plan(plan: str | None) -> bool:
    """True for a real paid subscription tier (not 'free', not one-off like 'strike')."""
    normalized = tariffs.normalize_plan(plan)
    return normalized in tariffs.PLAN_LEVELS and normalized != "free"


async def activate_subscription(executor, user_id: int, plan: str, months: int) -> datetime | None:
    """Grant/extend a subscription and sync platform_users. Returns expires_at.

    Returns None (and does nothing) if `plan` is not a subscription tier — the
    caller is responsible for one-off products like 'strike'.
    `executor` may be an asyncpg Pool or Connection (to join a transaction).
    """
    normalized = tariffs.normalize_plan(plan)
    if not is_subscription_plan(normalized):
        log.warning(
            "billing.activate_subscription: non-subscription plan %r for user=%s — skipped",
            plan, user_id,
        )
        return None

    months = max(1, int(months or 1))

    expires = await executor.fetchval(_SUBSCRIPTION_UPSERT, user_id, normalized, str(months))
    if expires is None:
        # RETURNING should always yield a row on a successful upsert; guard so a
        # freak None never breaks the caller's confirmation message.
        expires = datetime.now(timezone.utc) + timedelta(days=30 * months)

    # Keep platform_users in lockstep with the authoritative subscription expiry.
    try:
        await executor.execute(_PLATFORM_USERS_SYNC, user_id, normalized, expires)
    except Exception:
        log.warning(
            "billing.activate_subscription: platform_users sync failed for user=%s",
            user_id, exc_info=True,
        )

    try:
        from bot.utils.subscription import invalidate_plan_cache

        invalidate_plan_cache(user_id)
    except Exception:
        log.warning("billing.activate_subscription: cache invalidation failed", exc_info=True)

    return expires
