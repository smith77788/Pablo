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


def _note_write_cancelled(where: str, ctx: str, lost: str) -> None:
    """Отмена съела защитную запись. Разбор и счётчик — у общей двери.

    Писатели защиты ловят `except Exception`, а `asyncio.CancelledError` с
    Python 3.8 не `Exception`: отмена проходила сквозь них без следа, и слепы
    они оказывались ровно к нашим собственным остановкам (деплой, передача
    очереди другому процессу). Отмену вызывающий обязан пробросить сразу
    после этого вызова.
    """
    try:
        from services import metrics as _m

        _m.note_write_cancelled(__name__, where, ctx, lost)
    except Exception:
        pass

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


class BudgetUnreadable(RuntimeError):
    """Счёт действий за сутки не прочитался — считать аккаунты свободными нельзя."""


async def actions_today_bulk(
    pool: asyncpg.Pool, account_ids: Iterable[int]
) -> dict[int, int]:
    """Счётчик действий за 24ч для набора аккаунтов одним запросом.

    Бросает `BudgetUnreadable`, если счёт не прочитался и после повтора:
    пустой счёт означал бы «сделано 0» и полный бюджет каждому аккаунту
    заново. Решают вызывающие (`filter_within_budget`, `remaining_bulk`) — они
    переводят отказ в «бюджет исчерпан», и операция ждёт, а не перебирает."""
    ids = [int(a) for a in account_ids]
    if not ids:
        return {}
    counts: dict[int, int] = {}
    for _attempt in (1, 2):
        try:
            rows = await pool.fetch(_COUNT_SQL, ids, list(_COUNTED_ACTIONS))
            counts = {int(r["account_id"]): int(r["n"]) for r in rows}
            break
        except Exception:
            if _attempt == 1:
                # Одна попытка про запас: сбой чтения здесь выключает суточный
                # лимит целиком (ниже «сделано 0»), а сетевой блип до БД —
                # самая частая его причина.
                log.warning("бюджет: счёт действий за сутки не прочитался "
                            "(%d аккаунтов), повторяю", len(ids))
                await asyncio.sleep(0.5)
                continue
            # ЗДЕСЬ ЛОМАЕТСЯ ЗАЩИТА, А НЕ УЧЁТ. Пустой счёт читается как
            # «сделано 0», то есть на этот прогон суточный лимит аккаунта
            # перестаёт существовать: фильтр пропускает всех, остаток
            # считается полным. Прежнее решение остаться fail-open стоило
            # ровно того, от чего лимит и защищает — перебора и бана, причём
            # на фоне общей деградации базы, когда рисковать хуже всего.
            # Теперь отказ: вызывающие считают такие аккаунты исчерпавшими
            # бюджет, и операция ждёт следующего круга.
            log.error("бюджет: счёт действий за сутки НЕ ПРОЧИТАН (%d аккаунтов) "
                      "— аккаунты считаем исчерпавшими лимит, операция подождёт",
                      len(ids), exc_info=True)
            try:
                from services import metrics as _m

                _m.inc("infragram_daily_limit_read_failures_total",
                       {"where": "budget"})
            except Exception:
                log.debug("бюджет: счётчик сбоя чтения недоступен")
            raise BudgetUnreadable(
                "счёт действий за сутки недоступен, аккаунты не выдаём")
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
    try:
        counts = await actions_today_bulk(pool, ids)
    except BudgetUnreadable:
        # Не знаем, сколько аккаунты уже сделали, — значит не выдаём ни одного.
        # Вызывающие это умеют: пустой within читается как «работать некем», и
        # операция откладывается ровно как при занятом флоте.
        return [], ids
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
    сколько угодно. Сбой чтения → остаток НОЛЬ (как и у фильтра): прежний
    «сделано 0» выдавал полный бюджет заново, то есть лимит переставал
    существовать ровно тогда, когда база подрагивает.
    """
    ids = [int(a) for a in account_ids]
    if limit is None:
        limit = await get_daily_budget(pool)
    if limit <= 0:
        return {i: None for i in ids}
    try:
        counts = await actions_today_bulk(pool, ids)
    except BudgetUnreadable:
        return {i: 0 for i in ids}
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
        except asyncio.CancelledError:
            _note_write_cancelled(
                "budget", f"[acc={account_id} action={action}]",
                "суточный лимит недосчитает эти действия, и следующая "
                "операция может взять аккаунт сверх него")
            raise
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
