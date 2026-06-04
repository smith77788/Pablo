"""Gift Transfer Service — create and manage gift transfer plans."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class GiftTransferItem:
    """Single gift item in a transfer plan."""
    inventory_id: int
    account_id: int
    gift_id: str
    gift_type: str
    stars_cost: int
    status: str = "pending"
    error_message: str = ""
    error_code: str = ""
    is_retryable: bool = True
    retry_count: int = 0
    max_retries: int = 3


@dataclass
class GiftTransferPlan:
    """Complete gift transfer plan."""
    plan_id: int
    owner_id: int
    recipient_username: str
    recipient_user_id: int
    recipient_name: str
    payment_source: str
    payment_method_id: int = 0
    status: str = "pending"
    items: list[GiftTransferItem] = field(default_factory=list)
    total_gifts: int = 0
    selected_gifts: int = 0
    estimated_cost: int = 0
    accounts_used: list[dict] = field(default_factory=list)


class GiftTransferService:
    """Service for creating and managing gift transfer plans."""
    
    @staticmethod
    async def create_plan(
        pool,
        owner_id: int,
        recipient_username: str,
        recipient_user_id: int,
        recipient_name: str,
        payment_source: str,
        payment_method_id: int = 0
    ) -> int:
        """Create a new gift transfer plan. Returns plan_id."""
        row = await pool.fetchrow("""
            INSERT INTO gift_transfer_plans 
            (owner_id, recipient_username, recipient_user_id, recipient_name, 
             payment_source, payment_method_id, status)
            VALUES ($1, $2, $3, $4, $5, $6, 'pending')
            RETURNING id
        """, owner_id, recipient_username, recipient_user_id, recipient_name,
            payment_source, payment_method_id)
        return row["id"]
    
    @staticmethod
    async def add_items_to_plan(
        pool, plan_id: int, items: list[dict]
    ) -> int:
        """Add gift items to a transfer plan. Returns count added."""
        count = 0
        for item in items:
            try:
                await pool.execute("""
                    INSERT INTO gift_transfer_items 
                    (plan_id, inventory_id, account_id, gift_id, gift_type, stars_cost, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                    ON CONFLICT (plan_id, inventory_id) DO NOTHING
                """, plan_id, item["inventory_id"], item["account_id"],
                    item["gift_id"], item["gift_type"], item.get("stars_cost", 0))
                count += 1
            except Exception as e:
                log.error("add_items: failed for inventory_id %d: %s", 
                         item.get("inventory_id"), str(e)[:200])
        return count
    
    @staticmethod
    async def validate_plan(pool, plan_id: int) -> dict:
        """Validate a transfer plan: check recipient, payment source, gift validity."""
        errors = []
        warnings = []
        
        plan = await pool.fetchrow("SELECT * FROM gift_transfer_plans WHERE id=$1", plan_id)
        if not plan:
            return {"valid": False, "errors": ["Plan not found"]}
        
        # Check recipient
        if not plan["recipient_user_id"] and not plan["recipient_username"]:
            errors.append("No recipient specified")
        
        # Check items exist
        items_count = await pool.fetchval(
            "SELECT COUNT(*) FROM gift_transfer_items WHERE plan_id=$1", plan_id
        )
        if items_count == 0:
            errors.append("No gifts selected for transfer")
        
        # Check transferable status
        non_transferable = await pool.fetchval("""
            SELECT COUNT(*) FROM gift_transfer_items i
            JOIN gift_inventory g ON g.id = i.inventory_id
            WHERE i.plan_id=$1 AND g.is_transferable=false
        """, plan_id)
        if non_transferable > 0:
            warnings.append(f"{non_transferable} gifts are not transferable and will be skipped")
        
        # Check payment source availability
        payment_ok = await GiftTransferService._check_payment_source(
            pool, plan["owner_id"], plan["payment_source"], plan["payment_method_id"]
        )
        if not payment_ok:
            warnings.append("Payment source may not be available - transfers may require manual confirmation")
        
        # Update plan status
        if errors:
            await pool.execute(
                "UPDATE gift_transfer_plans SET status='validated', error_message=$2 WHERE id=$1",
                plan_id, "; ".join(errors)
            )
        else:
            await pool.execute(
                "UPDATE gift_transfer_plans SET status='validated' WHERE id=$1", plan_id
            )
        
        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "items_count": items_count,
            "non_transferable": non_transferable
        }
    
    @staticmethod
    async def _check_payment_source(pool, owner_id: int, payment_source: str, method_id: int) -> bool:
        """Check if payment source is available."""
        if payment_source == "stars":
            # Check if any account has stars balance
            # For now, assume true - actual check happens during transfer
            return True
        elif payment_source == "wallet":
            # Check for connected @wallet
            wallet = await pool.fetchrow("""
                SELECT id FROM user_payment_methods 
                WHERE owner_id=$1 AND method_type='wallet' 
                LIMIT 1
            """, owner_id)
            return wallet is not None
        elif payment_source == "saved_method":
            method = await pool.fetchrow(
                "SELECT id FROM user_payment_methods WHERE id=$1", method_id
            )
            return method is not None
        return True
    
    @staticmethod
    async def get_plan(pool, plan_id: int) -> dict | None:
        """Get transfer plan with all items."""
        plan = await pool.fetchrow(
            "SELECT * FROM gift_transfer_plans WHERE id=$1", plan_id
        )
        if not plan:
            return None
        
        items = await pool.fetch(
            "SELECT * FROM gift_transfer_items WHERE plan_id=$1", plan_id
        )
        
        return {
            **dict(plan),
            "items": [dict(i) for i in items]
        }
    
    @staticmethod
    async def build_preview(pool, plan_id: int) -> dict:
        """Build a preview of the transfer plan."""
        plan = await GiftTransferService.get_plan(pool, plan_id)
        if not plan:
            return {"error": "Plan not found"}
        
        transferable_items = [i for i in plan["items"] if i.get("status") == "pending"]
        non_transferable = await pool.fetchval("""
            SELECT COUNT(*) FROM gift_transfer_items i
            JOIN gift_inventory g ON g.id = i.inventory_id
            WHERE i.plan_id=$1 AND g.is_transferable=false
        """, plan_id)
        
        # Estimated cost
        total_cost = sum(i.get("stars_cost", 0) for i in transferable_items)
        
        # Accounts used
        account_ids = set(i["account_id"] for i in transferable_items)
        accounts_data = []
        for acc_id in account_ids:
            acc = await pool.fetchrow(
                "SELECT id, phone FROM tg_accounts WHERE id=$1", acc_id
            )
            if acc:
                count = sum(1 for i in transferable_items if i["account_id"] == acc_id)
                accounts_data.append({
                    "account_id": acc_id,
                    "phone": acc["phone"],
                    "gifts_count": count
                })
        
        return {
            "plan_id": plan_id,
            "status": plan["status"],
            "recipient": {
                "username": plan["recipient_username"],
                "user_id": plan["recipient_user_id"],
                "name": plan["recipient_name"]
            },
            "payment_source": plan["payment_source"],
            "total_gifts_selected": len(plan["items"]),
            "transferable_gifts": len(transferable_items),
            "non_transferable_gifts": non_transferable,
            "estimated_cost": total_cost,
            "accounts_used": len(account_ids),
            "accounts_data": accounts_data
        }
    
    @staticmethod
    async def update_item_status(
        pool, item_id: int, status: str,
        error_message: str = "", error_code: str = "",
        is_retryable: bool = True
    ) -> None:
        """Update status of a single transfer item."""
        await pool.execute("""
            UPDATE gift_transfer_items SET
                status = $2,
                error_message = COALESCE(NULLIF($3, ''), error_message),
                error_code = COALESCE(NULLIF($4, ''), error_code),
                is_retryable = $5,
                transferred_at = CASE WHEN $2 = 'transferred' THEN now() ELSE transferred_at END,
                retry_count = CASE WHEN $2 = 'failed' THEN retry_count + 1 ELSE retry_count END,
                updated_at = now()
            WHERE id = $1
        """, item_id, status, error_message, error_code, is_retryable)
    
    @staticmethod
    async def mark_plan_running(pool, plan_id: int) -> None:
        """Mark plan as running."""
        await pool.execute(
            "UPDATE gift_transfer_plans SET status='running', updated_at=now() WHERE id=$1",
            plan_id
        )
    
    @staticmethod
    async def mark_plan_done(pool, plan_id: int, actual_cost: int = 0) -> None:
        """Mark plan as done."""
        await pool.execute("""
            UPDATE gift_transfer_plans SET 
                status='done', 
                actual_cost=$2,
                completed_at=now(),
                updated_at=now()
            WHERE id=$1
        """, plan_id, actual_cost)
    
    @staticmethod
    async def get_retryable_items(pool, plan_id: int) -> list[dict]:
        """Get items eligible for retry."""
        rows = await pool.fetch("""
            SELECT * FROM gift_transfer_items
            WHERE plan_id=$1 AND status='failed' AND is_retryable=true AND retry_count < max_retries
        """, plan_id)
        return [dict(r) for r in rows]
    
    @staticmethod
    async def reset_failed_for_retry(pool, plan_id: int) -> int:
        """Reset failed retryable items for another attempt. Returns count reset."""
        result = await pool.execute("""
            UPDATE gift_transfer_items SET
                status = 'pending',
                error_message = '',
                error_code = '',
                updated_at = now()
            WHERE plan_id=$1 AND status='failed' AND is_retryable=true AND retry_count < max_retries
        """, plan_id)
        
        parts = result.split()
        return int(parts[2]) if len(parts) >= 3 else 0