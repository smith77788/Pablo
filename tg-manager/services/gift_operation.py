"""Gift Transfer Operation execution via Operation Engine."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

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
    
    # Get plan and items
    plan = await GiftTransferService.get_plan(pool, plan_id)
    if not plan:
        log.error("_exec_gift_transfer: plan %d not found", plan_id)
        return
    
    # Mark plan as running
    await GiftTransferService.mark_plan_running(pool, plan_id)
    
    # Link operation to plan
    await pool.execute("""
        INSERT INTO gift_transfer_ops (plan_id, operation_id, total_items)
        VALUES ($1, $2, $3)
    """, plan_id, op_id, len(plan["items"]))
    
    # Get pending items (filter out non-transferable)
    pending_items = []
    for item in plan["items"]:
        if item["status"] in ("pending", "queued"):
            # Check if gift is transferable
            inv = await pool.fetchrow(
                "SELECT is_transferable FROM gift_inventory WHERE id=$1",
                item["inventory_id"]
            )
            if inv and inv["is_transferable"]:
                pending_items.append(item)
            else:
                await GiftTransferService.update_item_status(
                    pool, item["id"], "skipped",
                    error_message="Gift is not transferable",
                    error_code=ERR_NOT_TRANSFERABLE,
                    is_retryable=False
                )
    
    # Execute transfers
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
                pool, item["id"], "failed",
                error_message=result.error_message,
                error_code=result.error_code,
                is_retryable=result.is_retryable
            )
        
        # Update operation progress
        await pool.execute("""
            UPDATE gift_transfer_ops SET
                transferred = $2,
                failed = $3,
                updated_at = now()
            WHERE plan_id=$1
        """, plan_id, transferred_count, failed_count)
    
    # Mark plan as done
    await GiftTransferService.mark_plan_done(pool, plan_id, total_cost)
    
    # Update operation stats
    await pool.execute("""
        UPDATE operation_queue SET
            completed_at = now(),
            status = 'done',
            total_items = $2,
            processed_items = $3,
            failed_items = $4
        WHERE id = $1
    """, op_id, len(plan["items"]), transferred_count, failed_count)
    
    # Generate final report
    report_id = await GiftTransferReportService.generate_report(pool, plan_id, op_id)
    log.info("_exec_gift_transfer: completed plan_id=%d report_id=%d transferred=%d failed=%d",
             plan_id, report_id, transferred_count, failed_count)


async def _transfer_single_gift(
    pool, item: dict, recipient_user_id: int, recipient_username: str
) -> GiftTransferResult:
    """Transfer a single gift to recipient.
    
    Returns GiftTransferResult with success/failure details.
    """
    from services import account_manager
    
    try:
        # Get account session
        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1", item["account_id"]
        )
        if not acc:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="Account not found",
                error_code=ERR_ACCOUNT_UNAVAILABLE,
                is_retryable=False
            )
        
        session = acc.get("session_str")
        if not session:
            return GiftTransferResult(
                item_id=item["id"],
                success=False,
                error_message="No session available",
                error_code=ERR_ACCOUNT_UNAVAILABLE,
                is_retryable=True
            )
        
        # Execute transfer via Telegram API
        # The exact API call depends on Telegram's current gift transfer API
        async with account_manager.get_client(session, acc) as client:
            # Try to transfer the gift
            # Note: This is a placeholder - actual Telegram API may differ
            try:
                result = await client.invoke(
                    lambda: client.transfer_star_gift(
                        gift_id=item["gift_id"],
                        to_peer=recipient_user_id or recipient_username
                    )
                )
                
                if result and getattr(result, "success", True):
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=True,
                        cost=item.get("stars_cost", 0)
                    )
                else:
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=False,
                        error_message=getattr(result, "error", "Transfer failed"),
                        error_code=ERR_TELEGRAM_API,
                        is_retryable=True
                    )
                    
            except Exception as api_err:
                err_str = str(api_err)
                
                # Categorize error
                if "balance" in err_str.lower() or "stars" in err_str.lower():
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=False,
                        error_message=err_str,
                        error_code=ERR_INSUFFICIENT_BALANCE,
                        is_retryable=True
                    )
                elif "confirm" in err_str.lower() or "external" in err_str.lower():
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=False,
                        error_message="Requires confirmation in Telegram app",
                        error_code=ERR_EXTERNAL_CONFIRM,
                        is_retryable=False  # Can't auto-retry external confirmations
                    )
                elif "rate" in err_str.lower() or "flood" in err_str.lower():
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=False,
                        error_message=err_str,
                        error_code=ERR_RATE_LIMIT,
                        is_retryable=True
                    )
                else:
                    return GiftTransferResult(
                        item_id=item["id"],
                        success=False,
                        error_message=err_str,
                        error_code=ERR_UNKNOWN,
                        is_retryable=True
                    )
                    
    except Exception as e:
        log.error("_transfer_single_gift: error for item %d: %s", item["id"], str(e)[:200])
        return GiftTransferResult(
            item_id=item["id"],
            success=False,
            error_message=str(e)[:200],
            error_code=ERR_SESSION_ERROR,
            is_retryable=True
        )