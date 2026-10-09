"""Дневной бюджет действий на аккаунт — человекоподобный лимит для долговечности.

Реальные (не-бот) аккаунты, которые совершают слишком много действий в сутки,
попадают под спам-фильтры Telegram. Warmup/ghost имеют свой pacing, но общего
лимита по ВСЕМ операциям (публикация/инвайт/накрутка) не было — этот модуль
считает действия аккаунта за сутки (по operation_audit) и позволяет исключать
«исчерпавшие бюджет» аккаунты из подбора.

Это не обход защит Telegram, а уважение к лимитам: меньше действий = дольше жизнь.
"""
from __future__ import annotations

import asyncio
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
    "post", "publish", "join", "leave", "report",
    "boost", "reaction", "view", "create_channel", "create_group",
    "profile", "story",
)

# Инвайты и ЛС — основной объём продукта, и у них СВОИ умные суточные лимиты:
# инвайт — recommended_daily_limit/progressive_daily_cap по истории аккаунта,
# ЛС-кампания — per_account_daily, разовая рассылка — per_account_cap. Общий
# бюджет в 50 действий их не режет (фактически не резал и раньше: построчных
# записей у них не было), иначе один день инвайтов закрывал бы аккаунту и
# публикации, и вступления. Построчно ЛС пишутся только для того, чтобы
# дневной лимит кампаний видел и разовые рассылки.
_OWN_LIMIT_ACTIONS = ("dm", "invite")

# Счёт бюджета — построчные записи operation_audit по риск-действиям.
_COUNT_SQL = """
    SELECT account_id, COUNT(*) AS n FROM operation_audit
     WHERE account_id = ANY($1::bigint[])
       AND occurred_at > now() - INTERVAL '24 hours'
       AND action = ANY($2::text[])
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
    """Записать n действий аккаунта в operation_audit построчно.

    Для исполнителей, которые раньше писали только итоговую строку операции:
    без построчной записи суточные лимиты их не видели. Никогда не бросает.

    ЭТО ЗАЩИТНАЯ ЗАПИСЬ, И ЕЁ ПОТЕРЯ НЕ МОЛЧИТ. Суточный бюджет считается по
    этим самым строкам, поэтому непрошедшая запись означает, что
    `filter_within_budget` видит у аккаунта меньше действий, чем он сделал, и
    следующая операция спокойно берёт его СВЕРХ суточного лимита — ровно тот
    риск спам-фильтра, от которого бюджет и придуман. Раньше сбой уходил в
    `log.debug`, то есть узнать о нём было негде.

    Одна повторная попытка здесь же: типовая причина мгновенная (занятый пул,
    блокировка на DDL, рестарт базы на деплое), и она проходит сама. Если и
    вторая не удалась — log.error и счётчик, рост которого сторож воркера
    доносит до владельца словами (op_worker._PROTECTIVE_FAILURE_COUNTERS).
    """
    if (not account_id or n <= 0
            or action not in _COUNTED_ACTIONS + _OWN_LIMIT_ACTIONS):
        return
    for _attempt in (1, 2):
        try:
            await pool.execute(
                """INSERT INTO operation_audit(owner_id, operation_id, account_id,
                                               action, result)
                   SELECT $1, $2, $3, $4, $5 FROM generate_series(1, $6)""",
                int(owner_id), operation_id, int(account_id), action, result,
                int(n),
            )
            return
        except Exception:
            if _attempt == 1:
                log.warning(
                    "бюджет: запись %d действий '%s' acc=%s не удалась, повторяю",
                    int(n), action, account_id)
                await asyncio.sleep(0.5)
                continue
            log.error(
                "бюджет: ЗАПИСЬ ПОТЕРЯНА acc=%s action=%s n=%d op=%s — суточный "
                "лимит недосчитает эти действия, и следующая операция может "
                "взять аккаунт сверх него",
                account_id, action, int(n), operation_id, exc_info=True)
            try:
                from services import metrics as _m

                _m.inc("infragram_budget_write_failures_total",
                       {"action": str(action)[:32]})
            except Exception:
                log.debug("бюджет: счётчик потери не записан", exc_info=True)
