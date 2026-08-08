"""Account readiness scoring for safe task admission.

The module scores real operational signals only: session availability, effective
account status, cooldown, proxy binding, recent operation success/failure, and
FloodWait history. It does not create artificial Telegram activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from services.account_manager import effective_account_status
from services.flood_engine import min_trust_for_action, normalize_trust_score


@dataclass(frozen=True)
class ReadinessResult:
    account_id: int
    score: float
    level: str
    allowed: bool
    reasons: tuple[str, ...]

    @property
    def normalized(self) -> float:
        return max(0.0, min(1.0, self.score / 100.0))


def readiness_level(score: float) -> str:
    if score < 25:
        return "blocked"
    if score < 40:
        return "raw"
    if score < 60:
        return "warming"
    if score < 80:
        return "ready"
    return "veteran"


def calculate_readiness(
    account: dict[str, Any],
    *,
    successes_7d: int = 0,
    failures_7d: int = 0,
    floods_7d: int = 0,
) -> ReadinessResult:
    account_id = int(account.get("id") or 0)
    has_session = bool(account.get("has_session")) or bool(
        account.get("session_str") or account.get("session_string")
    )
    is_active = bool(account.get("is_active", True))
    if not has_session:
        return ReadinessResult(account_id, 0.0, "blocked", False, ("no_session",))

    status = effective_account_status(
        account.get("acc_status"),
        has_session=has_session,
        is_active=is_active,
    )
    reasons: list[str] = []

    if status in {"archived", "banned", "deactivated", "no_session"}:
        return ReadinessResult(account_id, 0.0, "blocked", False, (status,))

    trust = normalize_trust_score(account.get("trust_score"))
    score = 35.0 + trust * 35.0

    total_ops = successes_7d + failures_7d
    if total_ops > 0:
        success_rate = successes_7d / total_ops
        score += success_rate * 20.0
        score -= min(20.0, failures_7d * 3.0)
    else:
        reasons.append("no_recent_ops")

    if account.get("proxy_id") and account.get("proxy_url"):
        score += 10.0
    else:
        score -= 15.0
        reasons.append("no_proxy")

    cooldown_until = account.get("cooldown_until")
    if cooldown_until:
        if isinstance(cooldown_until, datetime):
            cd = cooldown_until
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            if cd > datetime.now(timezone.utc):
                score = min(score, 35.0)
                reasons.append("cooldown")
        else:
            score = min(score, 35.0)
            reasons.append("cooldown")

    if status == "spamblock":
        score = min(score, 20.0)
        reasons.append("spamblock")

    if floods_7d:
        score -= min(35.0, floods_7d * 8.0)
        reasons.append("floods_7d")

    score = max(0.0, min(100.0, score))
    level = readiness_level(score)
    return ReadinessResult(account_id, score, level, score >= 40.0, tuple(reasons))


def is_ready_for_action(
    account: dict[str, Any],
    action_type: str,
    *,
    successes_7d: int = 0,
    failures_7d: int = 0,
    floods_7d: int = 0,
) -> bool:
    readiness = calculate_readiness(
        account,
        successes_7d=successes_7d,
        failures_7d=failures_7d,
        floods_7d=floods_7d,
    )
    return readiness.normalized >= min_trust_for_action(action_type)


async def refresh_account_readiness(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int | None = None,
) -> ReadinessResult | None:
    owner_filter = "AND a.owner_id=$2" if owner_id is not None else ""
    params: list[Any] = [account_id]
    if owner_id is not None:
        params.append(owner_id)
    row = await pool.fetchrow(
        f"""SELECT a.id, a.owner_id, a.is_active, a.session_str,
                   COALESCE(a.acc_status, 'active') AS acc_status,
                   a.cooldown_until, a.trust_score, a.proxy_id,
                   p.proxy_url,
                   COUNT(oa.id) FILTER (
                       WHERE oa.result='success'
                         AND oa.occurred_at > NOW() - INTERVAL '7 days'
                   )::int AS successes_7d,
                   COUNT(oa.id) FILTER (
                       WHERE oa.result IS NOT NULL
                         AND oa.result!='success'
                         AND oa.occurred_at > NOW() - INTERVAL '7 days'
                   )::int AS failures_7d,
                   COALESCE(a.flood_count_7d, 0)::int AS floods_7d
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            LEFT JOIN operation_audit oa ON oa.account_id = a.id
            WHERE a.id=$1 {owner_filter}
            GROUP BY a.id, p.proxy_url""",
        *params,
    )
    if not row:
        return None

    readiness = calculate_readiness(
        dict(row),
        successes_7d=int(row["successes_7d"] or 0),
        failures_7d=int(row["failures_7d"] or 0),
        floods_7d=int(row["floods_7d"] or 0),
    )

    current = normalize_trust_score(row["trust_score"])
    if readiness.level == "blocked":
        next_trust = min(current or 1.0, readiness.normalized)
    else:
        next_trust = max(current, readiness.normalized)

    await pool.execute(
        """UPDATE tg_accounts
           SET trust_score=$2,
               warmup_level=$3,
               status_reason=CASE
                   WHEN $4::text <> '' THEN $4
                   ELSE status_reason
               END
           WHERE id=$1""",
        account_id,
        next_trust,
        readiness.level,
        ", ".join(readiness.reasons),
    )
    return readiness
