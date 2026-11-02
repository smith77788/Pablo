"""Квота операций бесплатного тарифа.

ЗАЧЕМ. Доступ к операциям был бинарным: у каждой операции `min_plan`, и на
бесплатном тарифе нельзя было поставить ни одной. Новичок получал три дня
триала, а после них продукт выключался целиком — ни попробовать снова, ни
показать коллеге. У лидеров бесплатный уровень полезен сам по себе и упирается
в объём, а не в стену. Теперь владелец без подписки может поставить
`QUOTA_FREE_OPS_MONTH` операций в календарный месяц (по умолчанию 10).

ЧТО НЕ СЧИТАЕТСЯ. Прогрев (`account_warmup`): его работу делает фоновый цикл
каждый день, и гейт тарифа спрашивается перед каждым днём, а не при постановке —
квота на постановку дала бы план, который встаёт на второй день.

Счётчик растёт атомарно, одной строкой SQL с условием `used < limit`: две
одновременные постановки не проскочат последнюю единицу квоты вдвоём.
Fail-closed: не удалось посчитать — квоты нет (платный доступ по ошибке хуже,
чем отказ, который покажет путь к оплате).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

log = logging.getLogger(__name__)

# Операции, которые квота не покрывает (см. докстринг).
EXCLUDED_OPS = frozenset({"account_warmup"})
_DEFAULT = 10

_CONSUME_SQL = """
    INSERT INTO free_op_usage(owner_id, month, used) VALUES($1, $2, 1)
    ON CONFLICT (owner_id, month) DO UPDATE
       SET used = free_op_usage.used + 1, updated_at = now()
     WHERE free_op_usage.used < $3
    RETURNING used
"""


def monthly_limit() -> int:
    """Операций в месяц на бесплатном тарифе (env QUOTA_FREE_OPS_MONTH, 0 — выкл)."""
    raw = (os.getenv("QUOTA_FREE_OPS_MONTH") or "").strip()
    if not raw:
        return _DEFAULT
    try:
        return max(0, min(1000, int(raw)))
    except ValueError:
        log.warning("free_quota: bad QUOTA_FREE_OPS_MONTH=%r — using %s", raw, _DEFAULT)
        return _DEFAULT


def month_start(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def covers(op_type: str) -> bool:
    return op_type not in EXCLUDED_OPS and monthly_limit() > 0


async def consume(pool, owner_id: int, op_type: str, *, now: datetime | None = None) -> bool:
    """Списать одну операцию из квоты месяца. True — списано, можно ставить."""
    limit = monthly_limit()
    if limit <= 0 or not covers(op_type):
        return False
    try:
        used = await pool.fetchval(_CONSUME_SQL, owner_id, month_start(now), limit)
    except Exception:
        log.warning("free_quota: счётчик недоступен owner=%s — квоты нет", owner_id,
                    exc_info=True)
        return False
    return used is not None and 1 <= int(used) <= limit


async def refund(pool, owner_id: int, *, now: datetime | None = None) -> None:
    """Вернуть единицу квоты: операция после списания не встала в очередь."""
    try:
        await pool.execute(
            "UPDATE free_op_usage SET used = GREATEST(used - 1, 0), updated_at = now() "
            "WHERE owner_id=$1 AND month=$2", owner_id, month_start(now))
    except Exception:
        log.warning("free_quota: возврат не записан owner=%s", owner_id, exc_info=True)


async def remaining(pool, owner_id: int, *, now: datetime | None = None) -> int:
    """Сколько операций осталось в этом месяце (для экрана тарифа)."""
    limit = monthly_limit()
    try:
        used = await pool.fetchval(
            "SELECT used FROM free_op_usage WHERE owner_id=$1 AND month=$2",
            owner_id, month_start(now))
    except Exception:
        return 0
    return max(0, limit - int(used or 0))
