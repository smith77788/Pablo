"""
Resource Selector — единый механизм выбора аккаунтов и прокси.

Заменяет три конкурирующих паттерна:
  1. _get_active_accounts() из op_helpers — возвращает всё без flood-интеллекта
  2. flood_engine.get_best_account() — возвращает один лучший аккаунт
  3. geo_router — изолированный гео-специфичный выбор

Все системы должны использовать этот модуль вместо прямых SQL-запросов на tg_accounts.
"""

from __future__ import annotations

import logging
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
) -> list[asyncpg.Record]:
    """Вернуть все активные аккаунты (аналог _get_active_accounts, но с опцией фильтра cooldown).

    Используется когда нужны все аккаунты, а не один лучший.
    Для bulk-операций без flood-ранжирования.

    include_ids: если указан — вернуть только эти аккаунты (пользователь выбрал конкретные).
    """
    conditions = ["a.owner_id=$1", "a.is_active=TRUE", "a.session_str IS NOT NULL"]
    params: list = [owner_id]

    if respect_cooldown:
        conditions.append("(a.cooldown_until IS NULL OR a.cooldown_until < NOW())")

    if include_ids:
        params.append(include_ids)
        conditions.append(f"a.id = ANY(${len(params)})")

    if pool_name is not None:
        params.append(pool_name)
        conditions.append(f"a.pool=${len(params)}")

    if tags:
        params.append(tags)
        conditions.append(f"a.tags @> ${len(params)}::text[]")

    min_trust = (
        flood_engine.min_trust_for_action(action_type)
        if min_trust_score is None
        else min_trust_score
    )
    if min_trust > 0:
        params.append(min_trust)
        conditions.append(f"COALESCE(a.trust_score, 0) >= ${len(params)}")

    where = " AND ".join(conditions)
    return await pool.fetch(
        f"""SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                   a.device_model, a.system_version, a.app_version,
                   a.lang_code, a.system_lang_code, a.proxy_id,
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
