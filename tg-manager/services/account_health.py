"""
Account Health Engine — комплексная оценка здоровья аккаунтов.

Tracks:
- health_score (0-100): общая оценка надёжности аккаунта
- load_score (0-100): текущая нагрузка (чем выше, тем меньше использовать)
- warmup_state: raw/warming/ready/veteran
- restriction_history: история блокировок
- operation success/fail rates

Интегрируется с tg_accounts (trust_score, cooldown_until, acc_status),
account_flood_log, account_trust_history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


class WarmupState(str, Enum):
    RAW      = "raw"       # новый аккаунт, никогда не использовался
    WARMING  = "warming"   # в процессе разогрева
    READY    = "ready"     # готов к операциям
    VETERAN  = "veteran"   # проверенный, много успешных операций


@dataclass
class AccountHealth:
    account_id: int
    health_score: float = 100.0      # 0-100, выше = лучше
    load_score: float = 0.0          # 0-100, выше = более нагружен
    warmup_state: WarmupState = WarmupState.RAW
    total_ops: int = 0
    success_ops: int = 0
    fail_ops: int = 0
    flood_events_7d: int = 0
    spamblock_events: int = 0
    restriction_count: int = 0
    last_updated: float = field(default_factory=time.monotonic)
    suitability: dict[str, bool] = field(default_factory=lambda: {
        "invite": True,
        "dm": True,
        "create": True,
        "post": True,
        "join": True,
    })


# In-memory health cache
_health_cache: dict[int, AccountHealth] = {}


def get_health(account_id: int) -> AccountHealth:
    if account_id not in _health_cache:
        _health_cache[account_id] = AccountHealth(account_id=account_id)
    return _health_cache[account_id]


def compute_health_score(
    trust_score: float,
    flood_count_7d: int,
    spamblock: bool,
    success_rate: float,  # 0.0-1.0
    days_active: int,
) -> float:
    """Вычисляет health_score (0-100) по нескольким факторам."""
    score = 100.0

    # Trust score (0-1 → 0-30 points)
    score -= (1.0 - min(1.0, trust_score)) * 30

    # Flood penalties
    score -= min(flood_count_7d * 5, 30)

    # SpamBot restriction
    if spamblock:
        score -= 40

    # Success rate factor (0-20 points)
    score -= (1.0 - success_rate) * 20

    # Age bonus (up to +10 for accounts active 30+ days)
    score += min(days_active / 3.0, 10)

    return max(0.0, min(100.0, score))


def estimate_warmup_state(days_active: int, total_ops: int, trust_score: float) -> WarmupState:
    if trust_score < 0.2 or total_ops == 0:
        return WarmupState.RAW
    if days_active < 3 or total_ops < 10:
        return WarmupState.WARMING
    if trust_score >= 0.8 and total_ops >= 100:
        return WarmupState.VETERAN
    return WarmupState.READY


def update_after_success(account_id: int, action_type: str = "default") -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.success_ops += 1
    health.load_score = min(100.0, health.load_score + 1.0)
    # Small health boost on success
    health.health_score = min(100.0, health.health_score + 0.2)
    health.last_updated = time.monotonic()


def update_after_failure(
    account_id: int,
    action_type: str = "default",
    is_flood: bool = False,
    is_spamblock: bool = False,
    is_ban: bool = False,
) -> None:
    health = get_health(account_id)
    health.total_ops += 1
    health.fail_ops += 1

    if is_flood:
        health.flood_events_7d += 1
        health.health_score -= 5
        health.load_score += 10

    if is_spamblock:
        health.spamblock_events += 1
        health.health_score -= 25
        health.suitability["dm"] = False
        health.suitability["invite"] = False

    if is_ban:
        health.health_score = 0
        health.suitability = {k: False for k in health.suitability}

    health.health_score = max(0.0, health.health_score)
    health.last_updated = time.monotonic()


async def load_from_db(pool: asyncpg.Pool, owner_id: int) -> int:
    """Загружает статистику здоровья всех аккаунтов из БД."""
    rows = await pool.fetch(
        """SELECT
               a.id,
               a.trust_score,
               COALESCE(a.flood_count_7d, 0) AS flood_count_7d,
               COALESCE(a.acc_status, 'active') AS acc_status,
               EXTRACT(DAY FROM NOW() - a.added_at)::int AS days_active,
               COUNT(DISTINCT fl.id) FILTER (WHERE fl.created_at > NOW() - INTERVAL '7d') AS floods_7d,
               COUNT(DISTINCT ol.id) FILTER (WHERE ol.status='success') AS ops_ok,
               COUNT(DISTINCT ol.id) FILTER (WHERE ol.status!='success') AS ops_fail
           FROM tg_accounts a
           LEFT JOIN account_flood_log fl ON fl.account_id = a.id
           LEFT JOIN operation_log ol ON ol.account_id = a.id
           WHERE a.owner_id = $1
           GROUP BY a.id, a.trust_score, a.flood_count_7d, a.acc_status, a.added_at""",
        owner_id,
    )

    loaded = 0
    for row in rows:
        acc_id = row["id"]
        total = (row["ops_ok"] or 0) + (row["ops_fail"] or 0)
        success_rate = (row["ops_ok"] or 0) / max(total, 1)
        is_spamblock = row["acc_status"] in ("spamblock", "cooldown")

        score = compute_health_score(
            trust_score=float(row["trust_score"] or 1.0),
            flood_count_7d=row["floods_7d"] or 0,
            spamblock=is_spamblock,
            success_rate=success_rate,
            days_active=row["days_active"] or 0,
        )

        warmup = estimate_warmup_state(
            row["days_active"] or 0,
            total,
            float(row["trust_score"] or 1.0),
        )

        health = get_health(acc_id)
        health.health_score = score
        health.warmup_state = warmup
        health.total_ops = total
        health.success_ops = row["ops_ok"] or 0
        health.fail_ops = row["ops_fail"] or 0
        health.flood_events_7d = row["floods_7d"] or 0
        health.load_score = min(100.0, (row["floods_7d"] or 0) * 5.0)

        if is_spamblock:
            health.suitability["dm"] = False
            health.suitability["invite"] = False

        loaded += 1

    log.info("account_health: loaded %d accounts for owner=%d", loaded, owner_id)
    return loaded


async def get_sorted_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    limit: int = 20,
) -> list[dict]:
    """Возвращает аккаунты отсортированные по пригодности для action_type."""
    rows = await pool.fetch(
        """SELECT a.id, a.session_str, a.phone, a.first_name, a.trust_score,
                  a.device_model, a.system_version, a.app_version,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.owner_id = $1
             AND a.is_active = TRUE
             AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
             AND COALESCE(a.acc_status, 'active') NOT IN ('banned', 'deactivated', 'session_expired')
           ORDER BY a.trust_score DESC NULLS LAST
           LIMIT $2""",
        owner_id, limit * 2,
    )

    scored = []
    for row in rows:
        health = get_health(row["id"])
        # Проверяем пригодность для данного типа действия
        if action_type in health.suitability and not health.suitability[action_type]:
            continue
        scored.append((health.health_score - health.load_score / 2, dict(row)))

    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored[:limit]]


def get_health_summary(account_ids: list[int]) -> list[dict]:
    """Сводная таблица здоровья аккаунтов для UI."""
    result = []
    for acc_id in account_ids:
        h = get_health(acc_id)
        result.append({
            "account_id": acc_id,
            "health_score": round(h.health_score, 1),
            "load_score": round(h.load_score, 1),
            "warmup_state": h.warmup_state.value,
            "flood_events_7d": h.flood_events_7d,
            "success_rate": round(h.success_ops / max(h.total_ops, 1) * 100, 1),
            "suitability": h.suitability,
        })
    return sorted(result, key=lambda x: -x["health_score"])


async def run_health_check_loop(pool: asyncpg.Pool, interval_s: int = 3600) -> None:
    """Фоновый цикл: каждый час пересчитывает здоровье всех аккаунтов."""
    while True:
        try:
            owners = await pool.fetch(
                "SELECT DISTINCT owner_id FROM tg_accounts WHERE is_active=TRUE"
            )
            for row in owners:
                await load_from_db(pool, row["owner_id"])
        except Exception as e:
            log.warning("account_health loop error: %s", e)
        await asyncio.sleep(interval_s)
