"""Централизованный мутатор статуса аккаунта — единый источник правды смены
`tg_accounts.acc_status` (Ban Weather, Фаза 1).

Захват события делает триггер БД (trg_immunity_capture_status) — он ловит ЛЮБОЙ
путь смены статуса. Эта функция — предпочтительный путь: помимо смены статуса она
ОБОГАЩАЕТ только что записанное триггером событие человекочитаемой причиной,
источником и контекстом. Обогащение опционально для захвата, но повышает качество
автопсии. Сайты смены статуса мигрируют на неё постепенно (не критично для захвата).

См. docs/BAN_WEATHER_MODULE.md.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Словарь статусов: что считается «мёртвым» ───────────────────────────────
#
# Один набор на весь продукт. Раньше его выписывал каждый читатель сам, и
# наборы разошлись: единая дверь выбора аккаунтов (`resource_selector`) и экран
# флота считали мёртвым и `spamblock`, а риск-пульс
# (`infra_memory.get_account_health`) — нет. Аккаунт, получивший спам-блок
# после PEER_FLOOD, показывался владельцу ЗДОРОВЫМ ровно в том месте, которое
# для этого и сделано: операции его уже не брали, экран флота называл мёртвым,
# а приборный щиток — «здоров». Расхождение читается как «из 52 аккаунтов
# работают 20, а почему — непонятно».
#
# Набор — объединение всех прежних: исключать из работы аккаунт, помеченный
# `deleted` или `frozen`, тоже верно, и ослаблять защиту при сведении нельзя.
DEAD_STATUSES = frozenset({
    "banned", "deactivated", "session_expired", "spamblock", "deleted",
    "frozen",
})


def is_dead(acc_status: Optional[str]) -> bool:
    """Статус означает, что аккаунт для действий не годится (и сам не вернётся)."""
    return str(acc_status or "active").strip().lower() in DEAD_STATUSES


def sql_dead_list() -> str:
    """Готовый список для SQL: `NOT IN (sql_dead_list())`.

    Подставляется в текст запроса, поэтому значения — только из набора выше
    (литералы в коде, не пользовательский ввод).
    """
    return ", ".join(f"'{status}'" for status in sorted(DEAD_STATUSES))


async def set_status(
    pool,
    acc_id: int,
    new_status: str,
    *,
    reason: Optional[str] = None,
    source: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
) -> bool:
    """Сменить acc_status аккаунта и обогатить событие.

    Возвращает True, если статус реально изменился (событие записано триггером).
    Fail-soft: обогащение не критично — при сбое смена статуса всё равно в силе.
    """
    from services.logger import log_exc_swallow

    try:
        row = await pool.fetchrow(
            """UPDATE tg_accounts
               SET acc_status = $2
               WHERE id = $1 AND acc_status IS DISTINCT FROM $2
               RETURNING id""",
            acc_id, new_status,
        )
    except Exception:
        log_exc_swallow(log, f"account_status: смена статуса acc={acc_id} упала")
        return False

    if not row:
        return False  # статус не изменился — триггер не сработал, обогащать нечего

    if reason is None and source is None and context is None:
        return True

    # Обогащаем последнее (только что созданное триггером) событие для этого acc.
    try:
        await pool.execute(
            """UPDATE account_status_events
               SET reason = COALESCE($2, reason),
                   source = COALESCE($3, source),
                   context = COALESCE($4::jsonb, context)
               WHERE id = (
                   SELECT id FROM account_status_events
                   WHERE acc_id = $1 AND new_status = $5 AND processed_at IS NULL
                   ORDER BY id DESC LIMIT 1
               )""",
            acc_id, reason, source,
            json.dumps(context, ensure_ascii=False) if context is not None else None,
            new_status,
        )
    except Exception:
        log_exc_swallow(log, f"account_status: обогащение события acc={acc_id} упало")

    return True
