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


# Откуда берётся счёт. Раньше бюджет читал ТОЛЬКО operation_audit с action из
# _COUNTED_ACTIONS, а исполнители пишут туда по строке на действие лишь для
# вступления/выхода/публикации/реакции/просмотра. Итоговая строка операции
# несёт action=op_type («mass_invite», «bulk_dm_adhoc»…), которого в списке нет,
# — поэтому инвайты и ЛС, самые баноопасные действия, в бюджет не попадали
# вовсе: аккаунт мог разослать сотни ЛС и остаться «в бюджете».
# Теперь два источника:
#   * operation_audit — по строке на действие (ЛС пишутся через record_actions);
#   * account_daily_stats — суточные факты масс-инвайта (bump_daily_stats):
#     каждая попытка приглашения, успешная или нет, это запрос к Telegram.
# Инвайт в operation_audit по строкам не пишется, так что двойного счёта нет.
_COUNT_SQL = """
    SELECT account_id, SUM(n)::bigint AS n FROM (
        SELECT account_id, COUNT(*) AS n FROM operation_audit
         WHERE account_id = ANY($1::bigint[])
           AND occurred_at > now() - INTERVAL '24 hours'
           AND action = ANY($2::text[])
         GROUP BY account_id
        UNION ALL
        SELECT account_id,
               COALESCE(SUM(actions_ok + actions_fail), 0) AS n
          FROM account_daily_stats
         WHERE account_id = ANY($1::bigint[])
           AND stat_date = CURRENT_DATE
         GROUP BY account_id
    ) t
    GROUP BY account_id"""


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
    """Число риск-действий аккаунта за сутки (см. `_COUNT_SQL`)."""
    counts = await actions_today_bulk(pool, [account_id])
    return counts.get(int(account_id), 0)


async def actions_today_bulk(
    pool: asyncpg.Pool, account_ids: Iterable[int]
) -> dict[int, int]:
    """Счётчик действий за 24ч для набора аккаунтов одним запросом."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return {}
    try:
        rows = await pool.fetch(_COUNT_SQL, ids, list(_COUNTED_ACTIONS))
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


async def remaining_bulk(
    pool: asyncpg.Pool, account_ids: Iterable[int], limit: int | None = None
) -> dict[int, int | None]:
    """Сколько риск-действий каждому аккаунту ещё можно сделать сегодня.

    None — лимит выключен (без ограничения). Исполнитель обязан ограничивать
    объём ВНУТРИ прогона этим числом: отбор по `filter_within_budget` на старте
    пропускает аккаунт с остатком 1, и без счёта по ходу он делает за прогон
    сколько угодно. Сбой чтения → считаем, что сделано 0 (fail-open, как и
    фильтр): сбой учёта не должен останавливать операцию.
    """
    ids = [int(a) for a in account_ids]
    if limit is None:
        limit = await get_daily_budget(pool)
    if limit <= 0:
        return {i: None for i in ids}
    counts = await actions_today_bulk(pool, ids)
    return {i: max(0, limit - counts.get(i, 0)) for i in ids}


async def record_actions(
    pool: asyncpg.Pool,
    owner_id: int,
    account_id: int,
    action: str,
    n: int = 1,
    *,
    result: str = "ok",
    operation_id: int | None = None,
) -> None:
    """Записать n риск-действий аккаунта в operation_audit — так их увидит бюджет.

    Для исполнителей, которые раньше писали только итоговую строку операции
    (ЛС-рассылки): без построчной записи бюджет их не видел. Никогда не бросает.
    """
    if not account_id or n <= 0 or action not in _COUNTED_ACTIONS:
        return
    try:
        await pool.execute(
            """INSERT INTO operation_audit(owner_id, operation_id, account_id,
                                           action, result)
               SELECT $1, $2, $3, $4, $5 FROM generate_series(1, $6)""",
            int(owner_id), operation_id, int(account_id), action, result, int(n),
        )
    except Exception:
        log.debug("record_actions failed acc=%s", account_id, exc_info=True)
