"""Массовая рассылка ЛС (mass_dm) — эталонная массовая операция WB Chat.

Демонстрирует полный аккаунтный конвейер поверх абстрактного транспорта:
  • ротация аккаунтов (accounts.pick_account) — «здоровье», кулдаун, дневной бюджет;
  • пейсинг между отправками (pacing.Pacer) с адаптацией на flood и circuit breaker;
  • перезапускаемость: цели живут в wb_operation_targets, доделываем только pending;
  • корректная обработка ошибок транспорта (flood/invalid/protocol-unavailable).

Функция execute вызывается op_worker'ом. Логику массовой отправки можно проверить
на мок-транспорте целиком (см. tests), не касаясь сети.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg

from services.wb_chat import accounts
from services.wb_chat.pacing import Pacer
from services.wb_chat.session_manager import open_transport
from services.wb_chat.transport import (
    WBChatTransport,
    WBFloodWait,
    WBPeerInvalid,
    WBProtocolUnavailable,
)

log = logging.getLogger(__name__)

ACTION_TYPE = "dm"


async def _pending_targets(pool: asyncpg.Pool, op_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, ref FROM wb_operation_targets WHERE op_id=$1 AND status='pending' ORDER BY id",
        op_id,
    )
    return [dict(r) for r in rows]


async def _set_target(pool: asyncpg.Pool, target_id: int, status: str, *, account_id=None, error="") -> None:
    await pool.execute(
        """UPDATE wb_operation_targets
               SET status=$2, account_id=$3, error=$4, updated_at=NOW() WHERE id=$1""",
        target_id, status, account_id, error or None,
    )


async def execute(pool: asyncpg.Pool, op: dict, *, driver: str | None = None) -> dict:
    """Исполнить mass_dm. Возвращает сводку {sent, skipped, failed, remaining}.

    payload: {"text": str, "daily_budget"?: int}. Цели берутся из
    wb_operation_targets (их разворачивает op_worker при постановке в очередь)."""
    owner_id = op["owner_id"]
    payload = op.get("payload") or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise ValueError("mass_dm: пустой текст рассылки")
    daily_budget = int(payload.get("daily_budget") or accounts.DEFAULT_DAILY_BUDGET)

    targets = await _pending_targets(pool, op["id"])
    pacer = Pacer(ACTION_TYPE)
    # Кэш подключённых транспортов на время операции (аккаунт → transport).
    open_clients: dict[int, WBChatTransport] = {}
    sent = skipped = failed = 0

    try:
        for tgt in targets:
            if pacer.tripped:
                log.warning("wb_chat mass_dm op=%s: circuit breaker разомкнут — стоп", op["id"])
                break

            # Ротация: аккаунт вне кулдауна и в рамках дневного бюджета.
            account = await accounts.pick_account(
                pool, owner_id, action_type=ACTION_TYPE, daily_budget=daily_budget
            )
            if account is None:
                log.info("wb_chat mass_dm op=%s: нет доступных аккаунтов — пауза операции", op["id"])
                break  # остальные цели останутся pending → доделаем в следующий заход

            transport = await _client_for(open_clients, account, driver)

            try:
                await transport.send_message(tgt["ref"], text)
            except WBProtocolUnavailable:
                # Транспорт не готов — это не «цель плохая», а «драйвер не поставлен».
                # Прерываем всю операцию с внятной ошибкой (op_worker пометит failed).
                raise
            except WBFloodWait as e:
                # Аккаунт под ограничением: наказываем и оставляем цель pending
                # (в следующий раз возьмётся другой аккаунт).
                await accounts.penalize(pool, account["id"], seconds=e.seconds)
                pacer.on_flood(e.seconds)
                continue
            except WBPeerInvalid as e:
                await _set_target(pool, tgt["id"], "skipped", account_id=account["id"], error=str(e))
                skipped += 1
                pacer.on_success()  # адресат плох, но аккаунт здоров — не наказываем
            except Exception as e:  # noqa: BLE001 — прочие сбои транспорта
                await _set_target(pool, tgt["id"], "failed", account_id=account["id"], error=str(e))
                failed += 1
                pacer.on_error()
            else:
                await _set_target(pool, tgt["id"], "done", account_id=account["id"])
                await accounts.incr_budget(pool, account["id"], ACTION_TYPE)
                await accounts.mark_used(pool, account["id"])
                sent += 1
                pacer.on_success()

            await _bump_progress(pool, op["id"])
            await asyncio.sleep(pacer.next_delay())
    finally:
        for tr in open_clients.values():
            try:
                await tr.disconnect()
            except Exception:  # noqa: BLE001
                pass

    remaining = await pool.fetchval(
        "SELECT COUNT(*) FROM wb_operation_targets WHERE op_id=$1 AND status='pending'", op["id"]
    )
    return {"sent": sent, "skipped": skipped, "failed": failed, "remaining": int(remaining or 0)}


async def _client_for(
    cache: dict[int, WBChatTransport], account: dict, driver: str | None
) -> WBChatTransport:
    """Взять из кэша или подключить транспорт аккаунта (переиспользуем соединение)."""
    tr = cache.get(account["id"])
    if tr is None:
        tr = open_transport(account, driver=driver)
        await tr.connect()
        cache[account["id"]] = tr
    return tr


async def _bump_progress(pool: asyncpg.Pool, op_id: int) -> None:
    await pool.execute(
        """UPDATE wb_operations
               SET progress = (SELECT COUNT(*) FROM wb_operation_targets
                                   WHERE op_id=$1 AND status <> 'pending'),
                   updated_at = NOW()
               WHERE id=$1""",
        op_id,
    )
