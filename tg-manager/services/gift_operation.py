"""Gift Transfer Operation execution via Operation Engine."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30

# Error codes for gift transfers
ERR_INSUFFICIENT_BALANCE = "insufficient_balance"
ERR_NOT_TRANSFERABLE = "not_transferable"
ERR_RECIPIENT_INVALID = "recipient_invalid"
ERR_ACCOUNT_UNAVAILABLE = "account_unavailable"
ERR_SESSION_ERROR = "session_error"
ERR_PAYMENT_SOURCE = "payment_source_unavailable"
ERR_TELEGRAM_API = "telegram_api_error"
ERR_EXTERNAL_CONFIRM = "external_confirmation_required"
ERR_RATE_LIMIT = "rate_limit"
ERR_UNKNOWN = "unknown_error"


@dataclass
class GiftTransferResult:
    """Result of a single gift transfer."""

    item_id: int
    success: bool
    error_message: str = ""
    error_code: str = ""
    is_retryable: bool = True
    cost: int = 0


async def _exec_gift_transfer(pool, op_id: int, params: dict) -> dict:
    """Execute gift transfer operation via Operation Engine.

    This runs as a background operation - never call directly from handlers.
    """
    from services.gift_transfer import GiftTransferService
    from services.gift_report import GiftTransferReportService

    plan_id = params.get("plan_id")
    if not plan_id:
        log.error("_exec_gift_transfer: no plan_id")
        return {"status": "failed", "summary": "⚠️ gift_transfer: plan_id отсутствует в params"}

    log.info("_exec_gift_transfer: starting plan_id=%d op_id=%d", plan_id, op_id)

    plan = await GiftTransferService.get_plan(pool, plan_id)
    if not plan:
        log.error("_exec_gift_transfer: plan %d not found", plan_id)
        return {"status": "failed", "summary": f"⚠️ gift_transfer: план #{plan_id} не найден"}

    await GiftTransferService.mark_plan_running(pool, plan_id)

    # Размер цели объявляет общая дверь: она пишет его один раз, на первом
    # прогоне, и не уменьшает под владельцем на возобновлённом (там список
    # короче — уже переданные подарки из него выпадают).
    from services.op_worker import _declare_total_items

    await _declare_total_items(pool, op_id, len(plan["items"]))

    await pool.execute(
        """
        INSERT INTO gift_transfer_ops (plan_id, operation_id, total_items)
        VALUES ($1, $2, $3)
        """,
        plan_id,
        op_id,
        len(plan["items"]),
    )

    # Filter to transferable pending items
    pending_items = []
    for item in plan["items"]:
        if item["status"] in ("pending", "queued"):
            inv = await pool.fetchrow(
                "SELECT is_transferable FROM gift_inventory WHERE id=$1",
                item["inventory_id"],
            )
            if inv and inv["is_transferable"]:
                pending_items.append(item)
            else:
                await GiftTransferService.update_item_status(
                    pool,
                    item["id"],
                    "skipped",
                    error_message="Gift is not transferable",
                    error_code=ERR_NOT_TRANSFERABLE,
                    is_retryable=False,
                )

    transferred_count = 0
    failed_count = 0
    total_cost = 0
    # Подарки, по которым вопрос был закрыт до этого прогона (уже переданные,
    # непередаваемые): прогресс обязан их учитывать, иначе возобновлённый
    # прогон показывает владельцу меньше сделанного, чем есть.
    settled_before = len(plan["items"]) - len(pending_items)

    # Отмена доходит до исполнителя ОПРОСОМ: своей задачи у него нет, владелец
    # нажимает «Отменить» и ждёт, что необратимое прекратится. Передача подарка
    # необратима и тратит звёзды, поэтому спрашиваем перед КАЖДОЙ — как все
    # остальные исполнители (`_is_cancelled` в op_worker, десятки вызовов).
    from services.op_worker import _is_cancelled

    cancelled = False
    for item in pending_items:
        if await _is_cancelled(pool, op_id):
            cancelled = True
            log.info(
                "_exec_gift_transfer: отмена владельца plan_id=%d op_id=%d — "
                "передано %d, остальные подарки остаются у аккаунтов",
                plan_id, op_id, transferred_count,
            )
            break

        result = await _transfer_single_gift(
            pool, item, plan["recipient_user_id"], plan["recipient_username"]
        )

        if result.success:
            transferred_count += 1
            total_cost += result.cost
            await GiftTransferService.update_item_status(
                pool, item["id"], "transferred"
            )
        else:
            failed_count += 1
            await GiftTransferService.update_item_status(
                pool,
                item["id"],
                "failed",
                error_message=result.error_message,
                error_code=result.error_code,
                is_retryable=result.is_retryable,
            )

        await pool.execute(
            """
            UPDATE gift_transfer_ops SET
                transferred = $2,
                failed = $3,
                updated_at = now()
            WHERE plan_id=$1
            """,
            plan_id,
            transferred_count,
            failed_count,
        )
        # Счётчик операции двигается по каждому разобранному подарку: по нему
        # владелец видит прогресс, а сторож застоя — что работа идёт.
        await pool.execute(
            "UPDATE operation_queue SET done_items=$2 WHERE id=$1",
            op_id,
            settled_before + transferred_count + failed_count,
        )

    if cancelled:
        # План возвращается из 'running': иначе владелец видит остановленную
        # передачу вечно работающей и не может запустить её заново.
        await pool.execute(
            "UPDATE gift_transfer_plans SET status='cancelled', updated_at=now() "
            "WHERE id=$1 AND status='running'",
            plan_id,
        )
    else:
        await GiftTransferService.mark_plan_done(pool, plan_id, total_cost)

    # Терминальный статус операции здесь НЕ пишется. Все пути завершения идут
    # через одну дверь (`op_worker._op_closed_by_us`), и её UPDATE щадит уже
    # закрытую строку — так отмена владельца не превращается в «выполнено».
    # Раньше этот UPDATE стоял здесь без условия и последним: он затирал отмену,
    # а дверь после него получала ноль строк и об исходе не докладывала вовсе.

    report_id = await GiftTransferReportService.generate_report(pool, plan_id, op_id)
    log.info(
        "_exec_gift_transfer: completed plan_id=%d report_id=%d transferred=%d failed=%d",
        plan_id,
        report_id,
        transferred_count,
        failed_count,
    )
    return {
        # Отмену и исход решает вызывающий: `op_status.classify_final` судит по
        # счётчикам, а не по слову исполнителя. Ключ "ok" обязателен: алиасы
        # нормализатора (`sent`, `created`, `invited`…) слова "transferred" не
        # знают, поэтому две ушедшие передачи из трёх объявлялись провалом.
        "status": "cancelled" if cancelled else "done",
        "summary": (
            f"🛑 Передача остановлена: {transferred_count} / {len(plan['items'])}"
            if cancelled else
            f"✅ Подарки отправлены: {transferred_count} / {len(plan['items'])} "
            f"(не удалось: {failed_count})"
        ),
        "ok": transferred_count,
        "transferred": transferred_count,
        "failed": failed_count,
        "total": len(plan["items"]),
    }


async def _transfer_single_gift(
    pool, item: dict, recipient_user_id: int, recipient_username: str
) -> GiftTransferResult:
    """Transfer a single gift to recipient using Telethon TransferStarGiftRequest."""
    from telethon.tl.functions.payments import TransferStarGiftRequest
    from telethon.tl.types import InputSavedStarGiftUser
    from services import account_manager

    try:
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1", item["account_id"]
        )
        if not acc:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="Account not found",
                error_code=ERR_ACCOUNT_UNAVAILABLE,
                is_retryable=False,
            )

        session = acc.get("session_str")
        if not session:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="No session available",
                error_code=ERR_ACCOUNT_UNAVAILABLE,
                is_retryable=True,
            )

        # gift_id stores the msg_id of the SavedStarGift (from GetSavedStarGiftsRequest)
        try:
            msg_id = int(item["gift_id"])
        except (TypeError, ValueError):
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message=f"Invalid gift_id format: {item['gift_id']}",
                error_code=ERR_TELEGRAM_API,
                is_retryable=False,
            )

        recipient = recipient_user_id or recipient_username
        if not recipient:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="No recipient specified",
                error_code=ERR_RECIPIENT_INVALID,
                is_retryable=False,
            )

        client = account_manager._make_client(session, dict(acc))
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

            result = await asyncio.wait_for(
                client(
                    TransferStarGiftRequest(
                        stargift=InputSavedStarGiftUser(msg_id=msg_id),
                        to_id=await client.get_input_entity(recipient),
                    )
                ),
                timeout=_CONNECT_TIMEOUT,
            )

            if result:
                return GiftTransferResult(
                    item_id=item["id"],
                    success=True,
                    cost=item.get("stars_cost", 0) or 0,
                )
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="Empty response from Telegram",
                error_code=ERR_TELEGRAM_API,
                is_retryable=True,
            )

        except asyncio.TimeoutError:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="Timeout during transfer",
                error_code=ERR_RATE_LIMIT,
                is_retryable=True,
            )
        except Exception as api_err:
            err_str = str(api_err)
            if "BALANCE" in err_str.upper() or "STARS" in err_str.upper():
                code = ERR_INSUFFICIENT_BALANCE
            elif "FLOOD" in err_str.upper() or "RATE" in err_str.upper():
                code = ERR_RATE_LIMIT
            elif "USER_NOT_FOUND" in err_str or "PEER_ID_INVALID" in err_str:
                code = ERR_RECIPIENT_INVALID
            elif "GIFT_TRANSFER" in err_str.upper() or "NOT_ALLOWED" in err_str.upper():
                code = ERR_NOT_TRANSFERABLE
            else:
                code = ERR_TELEGRAM_API
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message=err_str[:300],
                error_code=code,
                is_retryable=code not in (ERR_RECIPIENT_INVALID, ERR_NOT_TRANSFERABLE),
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    except Exception as e:
        log.error(
            "_transfer_single_gift: error for item %d: %s", item["id"], str(e)[:200]
        )
        return GiftTransferResult(
            item_id=item["id"],
            success=False,
            error_message=str(e)[:200],
            error_code=ERR_SESSION_ERROR,
            is_retryable=True,
        )
