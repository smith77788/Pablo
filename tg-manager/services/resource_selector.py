"""Resource Selector — unified account and proxy selection mechanism.

Replaces three competing patterns:
  1. _get_active_accounts() from op_helpers — returns all without flood intelligence
  2. flood_engine.get_best_account() — returns single best account
  3. geo_router — isolated geo-specific selection

All systems should use this module instead of direct SQL queries on tg_accounts.

Usage:
    from services.resource_selector import select_account, select_accounts

    # Select single best account
    account = await select_account(pool, owner_id, action_type="invite")

    # Select multiple accounts for wave operations
    accounts = await select_accounts(pool, owner_id, count=5, action_type="strike")
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import asyncpg

from services import flood_engine

log = logging.getLogger(__name__)


async def select_account(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    *,
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    min_trust_score: float | None = None,
) -> dict | None:
    """Выбрать один лучший аккаунт с учётом flood-состояния и risk score.

    Делегирует в flood_engine.get_best_account() — централизованный алгоритм
    выбора аккаунта с учётом: trust_score, cooldown_until, consecutive floods,
    in-memory risk_score, action delays.
    """
    return await flood_engine.get_best_account(
        pool=pool,
        owner_id=owner_id,
        action_type=action_type,
        exclude_ids=exclude_ids,
        pool_name=pool_name,
        tags=tags,
        min_trust_score=min_trust_score,
    )


async def select_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
    count: int,
    action_type: str = "default",
    *,
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    min_trust_score: float | None = None,
) -> list[dict]:
    """Выбрать N лучших аккаунтов для операции (например, для нескольких волн Strike).

    Каждый следующий аккаунт выбирается из оставшихся с учётом flood-состояния.
    Уже выбранные аккаунты исключаются через exclude_ids.
    """
    selected: list[dict] = []
    excluded = list(exclude_ids or [])

    for _ in range(count):
        acc = await flood_engine.get_best_account(
            pool=pool,
            owner_id=owner_id,
            action_type=action_type,
            exclude_ids=excluded,
            pool_name=pool_name,
            tags=tags,
            min_trust_score=min_trust_score,
        )
        if acc is None:
            break
        selected.append(acc)
        excluded.append(acc["id"])

    log.debug(
        "resource_selector.select_accounts: owner=%d requested=%d got=%d action=%s",
        owner_id,
        count,
        len(selected),
        action_type,
    )
    return selected


async def select_for_wave(
    pool: asyncpg.Pool,
    owner_id: int,
    wave_size: int,
    wave_num: int = 0,
    action_type: str = "strike",
    *,
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    min_trust_score: float | None = None,
) -> list[dict]:
    """Выбрать аккаунты для конкретной волны операции.

    wave_num учитывается для ротации — в каждой волне используются свежие аккаунты.
    """
    accs = await select_accounts(
        pool=pool,
        owner_id=owner_id,
        count=wave_size,
        action_type=action_type,
        exclude_ids=exclude_ids,
        pool_name=pool_name,
        tags=tags,
        min_trust_score=min_trust_score,
    )
    log.info(
        "resource_selector: wave=%d size=%d got=%d accs action=%s owner=%d",
        wave_num,
        wave_size,
        len(accs),
        action_type,
        owner_id,
    )
    return accs


async def select_all_active(
    pool: asyncpg.Pool,
    owner_id: int,
    *,
    include_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
    respect_cooldown: bool = True,
    action_type: str = "default",
    min_trust_score: float | None = None,
    respect_daily_budget: bool = False,
    geo_country: str | None = None,
) -> list[asyncpg.Record]:
    """Вернуть все активные аккаунты (аналог _get_active_accounts, но с опцией фильтра cooldown).

    Используется когда нужны все аккаунты, а не один лучший.
    Для bulk-операций без flood-ранжирования.

    include_ids: если указан — вернуть только эти аккаунты (пользователь выбрал конкретные).
    respect_daily_budget: исключить аккаунты, исчерпавшие дневной бюджет действий
        (долговечность). Если исчерпали ВСЕ — возвращаем всех + warning (не рушим op).
    """
    conditions = ["a.owner_id=$1", "a.is_active=TRUE", "a.session_str IS NOT NULL",
                  # Мёртвые по статусу не годятся для действия. Раньше bulk-
                  # исполнители несли этот фильтр каждый у себя; после переноса
                  # выбора в единую дверь он обязан быть здесь, иначе миграция
                  # молча ослабила бы защиту (взяли бы banned с is_active=TRUE).
                  "COALESCE(a.acc_status, 'active') NOT IN ('banned', 'deactivated', 'session_expired', 'spamblock')"]
    params: list = [owner_id]

    if respect_cooldown:
        conditions.append("(a.cooldown_until IS NULL OR a.cooldown_until < NOW())")

    if include_ids is not None:
        # Различаем None (все аккаунты) и [] (никого): пустой список — это
        # «выбранных нет», и вернуть надо ПУСТО, а не весь флот. Иначе операция
        # на выбранных аккаунтах молча раскатилась бы по всему флоту.
        params.append(include_ids)
        conditions.append(f"a.id = ANY(${len(params)})")

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool=${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    if geo_country:
        # Гео-изоляция: только аккаунты, чей активный прокси в этой стране.
        # Условие на p.geo_country делает LEFT JOIN фактически INNER (без прокси
        # в стране — не проходит), что и нужно гео-выбору.
        params.append(geo_country)
        conditions.append(f"UPPER(p.geo_country) = UPPER(${len(params)})")

    min_trust = (
        flood_engine.min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"""SELECT a.id, a.owner_id, a.tg_user_id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id, a.cf_relay_url,
                   a.tags, a.pool, a.labels, a.warnings, a.project,
                   a.trust_score, a.cooldown_until,
                   COALESCE(a.acc_status, 'active') AS acc_status,
                   p.proxy_url, p.geo_country
            FROM tg_accounts a
            LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
            WHERE {where}
            ORDER BY a.trust_score DESC NULLS LAST, a.added_at""",
        *params,
    )

    if respect_daily_budget and rows:
        try:
            from services import account_budget
            ids = [int(r["id"]) for r in rows]
            within, over = await account_budget.filter_within_budget(pool, ids)
            if within and over:
                within_set = set(within)
                rows = [r for r in rows if int(r["id"]) in within_set]
            elif over and not within:
                # Все исчерпали лимит — не рушим операцию, но громко предупреждаем.
                log.warning(
                    "select_all_active: ВСЕ %d аккаунтов исчерпали дневной бюджет "
                    "действий — операция продолжится, но риск бана повышен (owner=%d)",
                    len(over), owner_id,
                )
        except Exception:
            log.debug("select_all_active: budget filter failed", exc_info=True)

    return rows


async def record_flood(
    pool: asyncpg.Pool,
    account_id: int,
    wait_seconds: int,
    action_type: str = "default",
    operation_id: Optional[int] = None,
) -> float:
    """Записать FloodWait в flood_engine и БД. Возвращает реальное cooldown-время."""
    return await flood_engine.record_flood(
        pool=pool,
        account_id=account_id,
        wait_seconds=wait_seconds,
        action_type=action_type,
        operation_id=operation_id,
    )


async def record_success(account_id: int, action_type: str = "default") -> None:
    """Записать успешное действие в flood_engine (снижает risk_score)."""
    await flood_engine.record_success(account_id=account_id, action_type=action_type)


def get_risk_summary(account_ids: list[int]) -> dict[int, dict]:
    """Получить сводку рисков для списка аккаунтов (из in-memory состояния)."""
    return flood_engine.get_risk_summary(account_ids)


def is_cooling(account_id: int) -> bool:
    """Проверить, находится ли аккаунт в cooldown."""
    return flood_engine.is_account_cooling(account_id)


def cooldown_seconds(account_id: int) -> float:
    """Сколько секунд осталось до готовности аккаунта."""
    return flood_engine.seconds_until_ready(account_id)


# ── Account Rotation (ротация аккаунтов) ─────────────────────────────────────
# Smart selection с учётом load balancing и trust_score.

# Usage tracking: account_id → {total_ops, last_used, success_rate}
_account_usage: dict[int, dict] = {}


async def select_account_rotated(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    *,
    exclude_ids: list[int] | None = None,
    pool_name: str | None = None,
    tags: list[str] | None = None,
) -> dict | None:
    """Select best account with load balancing.
    
    Balances between:
    - trust_score (higher = better)
    - usage count (lower = better, avoid overusing one account)
    - cooldown status (cooling accounts skipped)
    - success rate (higher = better)
    """
    candidates = await select_all_active(
        pool, owner_id,
        pool_name=pool_name, tags=tags,
        action_type=action_type,
        respect_cooldown=True,
    )
    if not candidates:
        return None
    
    # Filter excluded
    if exclude_ids:
        candidates = [c for c in candidates if c["id"] not in exclude_ids]
    if not candidates:
        return None
    
    # Score each candidate
    scored = []
    for acc in candidates:
        usage = _account_usage.get(acc["id"], {})
        trust = float(acc.get("trust_score") or 0.5)
        ops_done = usage.get("total_ops", 0)
        success_rate = usage.get("success_rate", 1.0)
        
        # Score formula: trust * 0.4 + (1 - usage_penalty) * 0.3 + success_rate * 0.3
        usage_penalty = min(ops_done / 100, 1.0)  # Normalize to 0-1
        score = trust * 0.4 + (1 - usage_penalty) * 0.3 + success_rate * 0.3
        
        scored.append((score, acc))
    
    # Sort by score descending
    scored.sort(key=lambda x: -x[0])
    
    best_acc = scored[0][1]
    _record_account_usage(best_acc["id"], True)
    
    log.debug(
        "resource_selector.select_account_rotated: owner=%d selected=%d score=%.2f action=%s",
        owner_id, best_acc["id"], scored[0][0], action_type,
    )
    return dict(best_acc)


def _record_account_usage(account_id: int, success: bool) -> None:
    """Record account usage for load balancing."""
    usage = _account_usage.setdefault(account_id, {
        "total_ops": 0, "successes": 0, "last_used": 0
    })
    usage["total_ops"] += 1
    if success:
        usage["successes"] += 1
    usage["last_used"] = time.time()
    # Update success rate
    total = usage["total_ops"]
    usage["success_rate"] = usage["successes"] / total if total > 0 else 1.0


def get_account_usage_summary(account_ids: list[int]) -> dict[int, dict]:
    """Get usage summary for accounts."""
    return {
        aid: _account_usage.get(aid, {"total_ops": 0, "success_rate": 1.0})
        for aid in account_ids
    }
