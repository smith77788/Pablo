"""Gift Transfer Report generation."""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class GiftTransferReportService:
    """Service for generating gift transfer reports."""

    @staticmethod
    async def generate_report(
        pool, plan_id: int, operation_id: Optional[int] = None
    ) -> int:
        """Generate a final report for a gift transfer plan. Returns report_id."""
        from services.gift_transfer import GiftTransferService

        # Get plan data
        plan = await GiftTransferService.get_plan(pool, plan_id)
        if not plan:
            log.error("generate_report: plan %d not found", plan_id)
            return 0

        # Calculate stats
        total_selected = len(plan["items"])
        transferred = sum(1 for i in plan["items"] if i["status"] == "transferred")
        failed = sum(1 for i in plan["items"] if i["status"] == "failed")
        skipped = sum(1 for i in plan["items"] if i["status"] == "skipped")
        pending = sum(1 for i in plan["items"] if i["status"] == "pending_confirmation")

        # Get retryable failures
        retryable_items = await pool.fetch(
            """
            SELECT * FROM gift_transfer_items
            WHERE plan_id=$1 AND status='failed' AND is_retryable=true
        """,
            plan_id,
        )
        non_retryable_items = await pool.fetch(
            """
            SELECT * FROM gift_transfer_items
            WHERE plan_id=$1 AND status='failed' AND is_retryable=false
        """,
            plan_id,
        )

        # Group errors by category
        error_summary = {}
        failed_items = list(retryable_items) + list(non_retryable_items)
        for item in failed_items:
            code = item["error_code"] or "unknown"
            if code not in error_summary:
                error_summary[code] = []
            error_summary[code].append(
                {
                    "gift_id": item["gift_id"],
                    "gift_type": item["gift_type"],
                    "account_id": item["account_id"],
                    "error": item["error_message"],
                }
            )

        # Get accounts used
        account_ids = set(i["account_id"] for i in plan["items"])
        accounts_data = []
        for acc_id in account_ids:
            acc = await pool.fetchrow(
                "SELECT id, phone FROM tg_accounts WHERE id=$1", acc_id
            )
            if acc:
                gifts_count = sum(
                    1
                    for i in plan["items"]
                    if i["account_id"] == acc_id and i["status"] == "transferred"
                )
                accounts_data.append(
                    {
                        "account_id": acc_id,
                        "phone": acc["phone"],
                        "gifts_transferred": gifts_count,
                    }
                )

        # Calculate total cost
        total_cost = sum(
            i.get("stars_cost", 0)
            for i in plan["items"]
            if i["status"] == "transferred"
        )

        # Generate next actions
        next_actions = []
        if retryable_items:
            next_actions.append(
                {
                    "action": "retry",
                    "count": len(retryable_items),
                    "description": f"Retry {len(retryable_items)} failed transfers",
                }
            )
        if pending > 0:
            next_actions.append(
                {
                    "action": "check_external",
                    "count": pending,
                    "description": f"{pending} transfers pending external confirmation in Telegram",
                }
            )

        # Calculate gifts found (from inventory for this plan)
        inventory_count = await pool.fetchval(
            """
            SELECT COUNT(*) FROM gift_inventory i
            JOIN gift_transfer_items ti ON ti.inventory_id = i.id
            WHERE ti.plan_id=$1
        """,
            plan_id,
        )

        # Insert report
        report = await pool.fetchrow(
            """
            INSERT INTO gift_transfer_reports (
                plan_id, operation_id, owner_id,
                total_gifts_found, total_selected, transferred, failed, skipped, pending_confirmation,
                total_cost, currency,
                recipient_username, recipient_user_id, recipient_name,
                accounts_used, accounts_data,
                error_summary, retryable_failures, non_retryable,
                next_actions, completed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, now())
            RETURNING id
        """,
            plan_id,
            operation_id,
            plan["owner_id"],
            inventory_count,
            total_selected,
            transferred,
            failed,
            skipped,
            pending,
            total_cost,
            "stars",
            plan["recipient_username"],
            plan["recipient_user_id"],
            plan["recipient_name"],
            len(account_ids),
            accounts_data,
            error_summary,
            [i["id"] for i in retryable_items],
            [i["id"] for i in non_retryable_items],
            next_actions,
        )

        return report["id"] if report else 0

    @staticmethod
    async def get_report(pool, report_id: int) -> dict | None:
        """Get a specific report."""
        row = await pool.fetchrow(
            "SELECT * FROM gift_transfer_reports WHERE id=$1", report_id
        )
        return dict(row) if row else None

    @staticmethod
    async def get_reports_for_user(pool, owner_id: int, limit: int = 10) -> list[dict]:
        """Get recent reports for a user."""
        rows = await pool.fetch(
            """
            SELECT * FROM gift_transfer_reports
            WHERE owner_id=$1
            ORDER BY created_at DESC
            LIMIT $2
        """,
            owner_id,
            limit,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def get_report_for_plan(pool, plan_id: int) -> dict | None:
        """Get the report for a specific plan."""
        row = await pool.fetchrow(
            "SELECT * FROM gift_transfer_reports WHERE plan_id=$1", plan_id
        )
        return dict(row) if row else None
