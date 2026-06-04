"""Gift Inventory Service — scan and manage Telegram gifts across accounts."""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class GiftInventoryService:
    """Service for scanning and managing gift inventory from connected accounts."""
    
    @staticmethod
    async def scan_account_gifts(pool, account_id: int, owner_id: int) -> list[dict]:
        """Scan a single account for Telegram gifts.
        
        Uses Telegram client to fetch user's star gifts via getUserStarGifts.
        Returns list of gift dicts with all relevant metadata.
        """
        from services import account_manager
        
        gifts = []
        
        try:
            acc = await pool.fetchrow(
                "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                account_id, owner_id
            )
            if not acc:
                log.warning("scan_account_gifts: account %d not found", account_id)
                return []
            
            session = acc.get("session_str")
            if not session:
                log.warning("scan_account_gifts: no session for account %d", account_id)
                return []
            
            # Call Telegram API to get user star gifts
            # Using MTProto via account_manager
            try:
                async with account_manager.get_client(session, acc) as client:
                    # Get user's own star gifts
                    me = await client.get_me()
                    user_id = me.id
                    
                    # Fetch star gifts
                    gifts_result = await client.invoke(
                        lambda: client.get_user_star_gifts(user_id)
                    )
                    
                    if gifts_result:
                        gifts_data = gifts_result.gifts or []
                        
                        for gift in gifts_data:
                            gift_info = {
                                "account_id": account_id,
                                "gift_id": str(gift.id),
                                "gift_type": str(gift.stars_total or 0),
                                "slug": getattr(gift, "slug", ""),
                                "stars_cost": getattr(gift, "stars_cost", 0) or 0,
                                "is_transferable": getattr(gift, "can_be_transferred", False),
                                "is_premium": getattr(gift, "is_premium", False),
                                "is_unique": getattr(gift, "is_unique", False),
                                "is_limited": getattr(gift, "is_limited", False),
                                "limited_count": getattr(gift, "total_count", None),
                                "first_owner": getattr(gift, "first_peer_id", 0) == user_id,
                                "generation": getattr(gift, "generation", 1),
                            }
                            gifts.append(gift_info)
                            
            except Exception as e:
                log.error("scan_account_gifts: Telegram API error for account %d: %s", 
                         account_id, str(e)[:200])
                # Store empty result for this account
                
        except Exception as e:
            log.error("scan_account_gifts: error for account %d: %s", account_id, str(e)[:200])
        
        return gifts
    
    @staticmethod
    async def scan_multiple_accounts(pool, owner_id: int, account_ids: list[int]) -> list[dict]:
        """Scan multiple accounts for gifts concurrently."""
        import asyncio
        
        tasks = [
            GiftInventoryService.scan_account_gifts(pool, acc_id, owner_id)
            for acc_id in account_ids
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        all_gifts = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error("scan_accounts: failed for account %d: %s", account_ids[i], str(result)[:200])
            else:
                all_gifts.extend(result)
        
        return all_gifts
    
    @staticmethod
    async def sync_inventory_to_db(pool, owner_id: int, gifts: list[dict]) -> int:
        """Sync scanned gifts to gift_inventory table.
        
        Updates last_seen_at for existing gifts, adds new ones.
        Returns count of gifts synced.
        """
        synced = 0
        now = "now()"
        
        for gift in gifts:
            try:
                await pool.execute("""
                    INSERT INTO gift_inventory 
                    (owner_id, account_id, gift_id, gift_type, slug, stars_cost,
                     is_transferable, is_premium, is_unique, is_limited, limited_count,
                     first_owner, generation, last_seen_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                    ON CONFLICT (account_id, gift_id) DO UPDATE SET
                        last_seen_at = now(),
                        stars_cost = EXCLUDED.stars_cost,
                        gift_type = EXCLUDED.gift_type
                """, 
                    owner_id,
                    gift["account_id"],
                    gift["gift_id"],
                    gift.get("gift_type", ""),
                    gift.get("slug", ""),
                    gift.get("stars_cost", 0),
                    gift.get("is_transferable", True),
                    gift.get("is_premium", False),
                    gift.get("is_unique", False),
                    gift.get("is_limited", False),
                    gift.get("limited_count"),
                    gift.get("first_owner", False),
                    gift.get("generation", 1),
                )
                synced += 1
            except Exception as e:
                log.error("sync_inventory: failed for gift %s: %s", gift.get("gift_id"), str(e)[:200])
        
        return synced
    
    @staticmethod
    async def get_inventory_summary(pool, owner_id: int) -> dict:
        """Get inventory summary: total, transferable, non-transferable, by account."""
        try:
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1", owner_id
            )
            transferable = await pool.fetchval(
                "SELECT COUNT(*) FROM gift_inventory WHERE owner_id=$1 AND is_transferable=true", 
                owner_id
            )
            non_transferable = total - transferable
            
            # By account
            by_account = await pool.fetch("""
                SELECT 
                    a.id as account_id,
                    a.phone,
                    COUNT(*) as total_gifts,
                    SUM(CASE WHEN i.is_transferable THEN 1 ELSE 0 END) as transferable_count,
                    SUM(COALESCE(i.stars_cost, 0)) as total_stars_cost
                FROM gift_inventory i
                JOIN tg_accounts a ON a.id = i.account_id
                WHERE i.owner_id=$1
                GROUP BY a.id, a.phone
                ORDER BY total_gifts DESC
            """, owner_id)
            
            return {
                "total_gifts": total,
                "transferable_gifts": transferable,
                "non_transferable_gifts": non_transferable,
                "by_account": [
                    {
                        "account_id": r["account_id"],
                        "phone": r["phone"],
                        "total_gifts": r["total_gifts"],
                        "transferable": r["transferable_count"],
                        "total_stars_cost": r["total_stars_cost"],
                    }
                    for r in by_account
                ]
            }
        except Exception as e:
            log.error("get_inventory_summary: error: %s", str(e)[:200])
            return {
                "total_gifts": 0,
                "transferable_gifts": 0,
                "non_transferable_gifts": 0,
                "by_account": []
            }
    
    @staticmethod
    async def get_gifts_by_account(pool, owner_id: int, account_id: int) -> list[dict]:
        """Get all gifts for a specific account."""
        rows = await pool.fetch("""
            SELECT * FROM gift_inventory 
            WHERE owner_id=$1 AND account_id=$2
            ORDER BY stars_cost DESC
        """, owner_id, account_id)
        
        return [dict(r) for r in rows]
    
    @staticmethod
    async def clear_stale_inventory(pool, owner_id: int, hours_old: int = 24) -> int:
        """Remove gifts not seen in specified hours (except transferred ones)."""
        result = await pool.execute("""
            DELETE FROM gift_inventory 
            WHERE owner_id=$1 
            AND last_seen_at < now() - interval '1 hour' * $2
            AND id NOT IN (
                SELECT inventory_id FROM gift_transfer_items 
                WHERE status = 'transferred'
            )
        """, owner_id, hours_old)
        
        # Get count from result
        parts = result.split()
        return int(parts[2]) if len(parts) >= 3 else 0