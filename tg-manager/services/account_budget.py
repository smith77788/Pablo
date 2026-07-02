"""Дневной бюджет действий на аккаунт — человекоподобный лимит для долговечности.

Реальные (не-бот) аккаунты, которые совершают слишком много действий в сутки,
попадают под спам-фильтры Telegram. Warmup/ghost имеют свой pacing, но общего
лимита по ВСЕМ операциям (публикация/инвайт/накрутка) не было — этот модуль
считает действия аккаунта за сутки (по operation_audit) и позволяет исключать
«исчерпавшие бюджет» аккаунты из подбора.

Это не обход защит Telegram, а уважение к лимитам: меньше действий = дольше жизнь.
"""
from __future__ import annotations

import logging
from typing import Iterable

import asyncpg

log = logging.getLogger(__name__)

# Консервативный человекоподобный дефолт. Можно переопределить platform_setting
# 'account_daily_action_budget'. 0 или отрицательное = без лимита.
DEFAULT_DAILY_BUDGET = 50

# Действия, которые считаются «риск-действиями» (нагружают аккаунт).
# Пассивные (health-check, scan) в бюджет не входят.
_COUNTED_ACTIONS = (
    "post", "publish", "join", "leave", "dm", "invite", "report",
    "boost", "reaction", "view", "create_channel", "create_group",
    "profile", "story",
)


async def get_daily_budget(pool: asyncpg.Pool) -> int:
    """Дневной лимит действий на аккаунт (platform_setting или дефолт)."""
    try:
        from database.db import get_platform_setting
        raw = await get_platform_setting(pool, "account_daily_action_budget", "")
        if raw:
            return int(raw)
    except (TypeError, ValueError):
        pass
    except Exception:
        log.debug("get_daily_budget: settings read failed", exc_info=True)
    return DEFAULT_DAILY_BUDGET


async def actions_today(pool: asyncpg.Pool, account_id: int) -> int:
    """Число риск-действий аккаунта за последние 24 часа (по operation_audit)."""
    try:
        n = await pool.fetchval(
            """SELECT COUNT(*) FROM operation_audit
               WHERE account_id = $1
                 AND occurred_at > now() - INTERVAL '24 hours'
                 AND action = ANY($2::text[])""",
            account_id, list(_COUNTED_ACTIONS),
        )
        return int(n or 0)
    except Exception:
        log.debug("actions_today: query failed acc=%s", account_id, exc_info=True)
        return 0


async def actions_today_bulk(
    pool: asyncpg.Pool, account_ids: Iterable[int]
) -> dict[int, int]:
    """Счётчик действий за 24ч для набора аккаунтов одним запросом."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return {}
    try:
        rows = await pool.fetch(
            """SELECT account_id, COUNT(*) AS n FROM operation_audit
               WHERE account_id = ANY($1::bigint[])
                 AND occurred_at > now() - INTERVAL '24 hours'
                 AND action = ANY($2::text[])
               GROUP BY account_id""",
            ids, list(_COUNTED_ACTIONS),
        )
        counts = {int(r["account_id"]): int(r["n"]) for r in rows}
    except Exception:
        log.debug("actions_today_bulk: query failed", exc_info=True)
        counts = {}
    return {i: counts.get(i, 0) for i in ids}


async def filter_within_budget(
    pool: asyncpg.Pool, account_ids: Iterable[int], limit: int | None = None
) -> tuple[list[int], list[int]]:
    """Разделяет аккаунты на (в бюджете, исчерпавшие лимит).

    limit=None → берём из настроек. limit<=0 → лимит выключен (все проходят).
    Возвращает (within, over) — never пустой within без причины: если ВСЕ
    исчерпали лимит, вызывающий код сам решает (лучше отложить, чем сжечь).
    """
    ids = [int(a) for a in account_ids]
    if not ids:
        return [], []
    if limit is None:
        limit = await get_daily_budget(pool)
    if limit <= 0:
        return ids, []
    counts = await actions_today_bulk(pool, ids)
    within = [i for i in ids if counts.get(i, 0) < limit]
    over = [i for i in ids if counts.get(i, 0) >= limit]
    if over:
        log.info(
            "account_budget: %d/%d аккаунтов исчерпали дневной лимит (%d действий/сутки)",
            len(over), len(ids), limit,
        )
    return within, over
