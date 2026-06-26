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


async def _exec_gift_transfer(pool, op_id: int, params: dict) -> None:
    """Execute gift transfer operation via Operation Engine.

    This runs as a background operation - never call directly from handlers.
    """
    from services.gift_transfer import GiftTransferService
    from services.gift_report import GiftTransferReportService

    plan_id = params.get("plan_id")
    if not plan_id:
        log.error("_exec_gift_transfer: no plan_id")
        return

    log.info("_exec_gift_transfer: starting plan_id=%d op_id=%d", plan_id, op_id)

    plan = await GiftTransferService.get_plan(pool, plan_id)
    if not plan:
        log.error("_exec_gift_transfer: plan %d not found", plan_id)
        return

    await GiftTransferService.mark_plan_running(pool, plan_id)

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

    for item in pending_items:
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

    await GiftTransferService.mark_plan_done(pool, plan_id, total_cost)

    await pool.execute(
        """
        UPDATE operation_queue SET
            finished_at = now(),
            status = 'done',
            total_items = $2,
            done_items = $3
        WHERE id = $1
        """,
        op_id,
        len(plan["items"]),
        transferred_count,
    )

    report_id = await GiftTransferReportService.generate_report(pool, plan_id, op_id)
    log.info(
        "_exec_gift_transfer: completed plan_id=%d report_id=%d transferred=%d failed=%d",
        plan_id,
        report_id,
        transferred_count,
        failed_count,
    )
    return {
        "status": "done",
        "summary": (
            f"✅ Подарки отправлены: {transferred_count} / {len(plan['items'])} "
            f"(не удалось: {failed_count})"
        ),
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
