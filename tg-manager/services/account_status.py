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
