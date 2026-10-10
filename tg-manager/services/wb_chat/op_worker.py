"""Воркер очереди операций WB Chat (аналог services/op_worker.py у Telegram).

Забирает queued-операции из wb_operations и исполняет соответствующим движком.
Диспетчер op_type → движок расширяется одной строкой в OP_HANDLERS. Захват
операции атомарен (FOR UPDATE SKIP LOCKED) — безопасно при нескольких воркерах.

Транспорт пока за абстракцией: если активен реальный драйвер без протокола,
операция аккуратно падает в failed с внятной причиной (WBProtocolUnavailable),
а не «зависает молча».
"""

from __future__ import annotations

import asyncio
import json
import logging

import asyncpg

from services.logger import log_exc_swallow
from services.wb_chat.engines import mass_dm
from services.wb_chat.transport import WBProtocolUnavailable

log = logging.getLogger(__name__)

# Реестр движков: op_type → корутина execute(pool, op).
OP_HANDLERS = {
    "mass_dm": mass_dm.execute,
}

_POLL_INTERVAL = 3.0
_STARTUP_DELAY = 9.0


# ── Постановка в очередь ─────────────────────────────────────────────────────
async def enqueue(
    pool: asyncpg.Pool,
    *,
    owner_id: int,
    op_type: str,
    payload: dict,
    targets: list[str] | None = None,
) -> int:
    """Поставить операцию в очередь и развернуть её цели. Вернуть op_id.

    Разворачивание целей идемпотентно (уникальный индекс op_id+ref): повторная
    постановка того же списка не плодит дубликаты."""
    if op_type not in OP_HANDLERS:
        raise ValueError(f"неизвестный op_type: {op_type!r}")
    refs = [r.strip() for r in (targets or []) if r and r.strip()]
    op_id = await pool.fetchval(
        """INSERT INTO wb_operations (owner_id, op_type, payload, status, total)
               VALUES ($1, $2, $3::jsonb, 'queued', $4) RETURNING id""",
        owner_id, op_type, json.dumps(payload or {}), len(refs),
    )
    for ref in refs:
        await pool.execute(
            """INSERT INTO wb_operation_targets (op_id, ref)
                   VALUES ($1, $2) ON CONFLICT (op_id, ref) DO NOTHING""",
            op_id, ref,
        )
    return int(op_id)


# ── Захват и исполнение ──────────────────────────────────────────────────────
async def _claim_next(pool: asyncpg.Pool) -> dict | None:
    """Атомарно взять самую старую queued-операцию и перевести в running."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT id FROM wb_operations
                       WHERE status='queued'
                       ORDER BY created_at
                       FOR UPDATE SKIP LOCKED
                       LIMIT 1"""
            )
            if row is None:
                return None
            op = await conn.fetchrow(
                """UPDATE wb_operations
                       SET status='running', started_at=COALESCE(started_at, NOW()), updated_at=NOW()
                       WHERE id=$1
                       RETURNING *""",
                row["id"],
            )
    return _row_to_op(op)


def _row_to_op(row: asyncpg.Record | None) -> dict | None:
    if row is None:
        return None
    op = dict(row)
    payload = op.get("payload")
    if isinstance(payload, str):
        try:
            op["payload"] = json.loads(payload)
        except Exception:  # noqa: BLE001
            op["payload"] = {}
    return op


async def _finish(pool: asyncpg.Pool, op_id: int, status: str, *, result: dict | None = None, error: str = "") -> None:
    await pool.execute(
        """UPDATE wb_operations
               SET status=$2, result=$3::jsonb, error=$4, finished_at=NOW(), updated_at=NOW()
               WHERE id=$1""",
        op_id, status, json.dumps(result or {}), error or None,
    )


async def run_one(pool: asyncpg.Pool, op: dict, *, driver: str | None = None) -> None:
    """Исполнить одну операцию нужным движком, зафиксировать исход."""
    handler = OP_HANDLERS.get(op["op_type"])
    if handler is None:
        await _finish(pool, op["id"], "failed", error=f"нет движка для {op['op_type']}")
        return
    try:
        result = await handler(pool, op, driver=driver)
    except WBProtocolUnavailable as e:
        # Реальный протокол не поставлен — честно фиксируем причину, не глушим.
        log.warning("wb_chat op=%s: транспорт не готов: %s", op["id"], e)
        await _finish(pool, op["id"], "failed", error=str(e))
    except Exception as e:  # noqa: BLE001 — ошибка движка не должна убивать воркер
        log_exc_swallow(log, f"wb_chat op={op['id']} упала")
        await _finish(pool, op["id"], "failed", error=str(e))
    else:
        await _finish(pool, op["id"], "done", result=result)


# ── Фоновый цикл ─────────────────────────────────────────────────────────────
async def run(pool: asyncpg.Pool, main_bot=None) -> None:
    """Точка входа фонового сервиса (сигнатура как у прочих *.run для _resilient)."""
    await asyncio.sleep(_STARTUP_DELAY)
    log.info("wb_chat: воркер операций запущен")
    while True:
        try:
            op = await _claim_next(pool)
            if op is not None:
                await run_one(pool, op)
                continue  # сразу пробуем следующую, пока очередь не пуста
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            log_exc_swallow(log, "wb_chat: цикл воркера операций упал")
        await asyncio.sleep(_POLL_INTERVAL)
