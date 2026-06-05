"""Gift Inventory Service — scan and manage Telegram gifts across accounts."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30


class GiftInventoryService:
    """Service for scanning and managing gift inventory from connected accounts."""

    @staticmethod
    async def scan_account_gifts(pool, account_id: int, owner_id: int) -> list[dict]:
        """Scan a single account for saved Telegram Star Gifts.

        Uses GetSavedStarGiftsRequest (payments TL method, Telethon 1.36+).
        Returns list of gift dicts ready for sync_inventory_to_db.
        """
        from telethon.tl.functions.payments import GetSavedStarGiftsRequest
        from telethon.tl.types import InputPeerSelf
        from services import account_manager

        gifts: list[dict] = []

        acc = await pool.fetchrow(
            "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
            account_id,
            owner_id,
        )
        if not acc:
            log.warning("scan_account_gifts: account %d not found", account_id)
            return []

        session = acc.get("session_str")
        if not session:
            log.warning("scan_account_gifts: no session for account %d", account_id)
            return []

        client = account_manager._make_client(session, dict(acc))
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

            offset = ""
            while True:
                result = await asyncio.wait_for(
                    client(
                        GetSavedStarGiftsRequest(
                            peer=InputPeerSelf(),
                            offset=offset,
                            limit=100,
                        )
                    ),
                    timeout=_CONNECT_TIMEOUT,
                )

                for sg in result.gifts or []:
                    # sg is SavedStarGift; sg.gift is StarGift (or StarGiftUnique)
                    star_gift = sg.gift
                    is_unique = getattr(star_gift, "CONSTRUCTOR_ID", 0) != 0x313A9547

                    gifts.append(
                        {
                            "account_id": account_id,
                            # msg_id is used for InputSavedStarGiftUser in transfer calls
                            "gift_id": str(sg.msg_id)
                            if sg.msg_id
                            else str(sg.saved_id or ""),
                            "gift_type": getattr(star_gift, "title", "")
                            or str(getattr(star_gift, "stars", 0)),
                            "slug": getattr(star_gift, "auction_slug", "") or "",
                            "stars_cost": getattr(star_gift, "stars", 0) or 0,
                            # transferable if transfer_stars is set (even if 0)
                            "is_transferable": sg.transfer_stars is not None,
                            "is_premium": bool(
                                getattr(star_gift, "require_premium", False)
                            ),
                            "is_unique": is_unique,
                            "is_limited": bool(getattr(star_gift, "limited", False)),
                            "limited_count": getattr(
                                star_gift, "availability_total", None
                            ),
                            "first_owner": False,
                            "generation": 1,
                        }
                    )

                next_offset = getattr(result, "next_offset", None)
                if not next_offset:
                    break
                offset = next_offset

        except asyncio.TimeoutError:
            log.error("scan_account_gifts: timeout for account %d", account_id)
        except Exception as e:
            log.error(
                "scan_account_gifts: error for account %d: %s", account_id, str(e)[:300]
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        return gifts

    @staticmethod
    async def scan_multiple_accounts(
        pool, owner_id: int, account_ids: list[int]
    ) -> list[dict]:
        """Scan multiple accounts for gifts concurrently."""
        tasks = [
            GiftInventoryService.scan_account_gifts(pool, acc_id, owner_id)
            for acc_id in account_ids
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_gifts: list[dict] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error(
                    "scan_accounts: failed for account %d: %s",
                    account_ids[i],
                    str(result)[:200],
                )
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

        for gift in gifts:
            try:
                await pool.execute(
                    """
                    INSERT INTO gift_inventory
                    (owner_id, account_id, gift_id, gift_type, slug, stars_cost,
                     is_transferable, is_premium, is_unique, is_limited, limited_count,
                     first_owner, generation, last_seen_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                    ON CONFLICT (account_id, gift_id) DO UPDATE SET
                        last_seen_at = now(),
                        stars_cost = EXCLUDED.stars_cost,
                        gift_type = EXCLUDED.gift_type,
                        is_transferable = EXCLUDED.is_transferable
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
                log.error(
                    "sync_inventory: failed for gift %s: %s",
                    gift.get("gift_id"),
                    str(e)[:200],
                )

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
                owner_id,
            )
            non_transferable = (total or 0) - (transferable or 0)

            by_account = await pool.fetch(
                """
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
                """,
                owner_id,
            )

            return {
                "total_gifts": total or 0,
                "transferable_gifts": transferable or 0,
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
                ],
            }
        except Exception as e:
            log.error("get_inventory_summary: error: %s", str(e)[:200])
            return {
                "total_gifts": 0,
                "transferable_gifts": 0,
                "non_transferable_gifts": 0,
                "by_account": [],
            }

    @staticmethod
    async def get_gifts_by_account(pool, owner_id: int, account_id: int) -> list[dict]:
        """Get all gifts for a specific account."""
        rows = await pool.fetch(
            """
            SELECT * FROM gift_inventory
            WHERE owner_id=$1 AND account_id=$2
            ORDER BY stars_cost DESC
            """,
            owner_id,
            account_id,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def clear_stale_inventory(pool, owner_id: int, hours_old: int = 24) -> int:
        """Remove gifts not seen in specified hours (except transferred ones)."""
        result = await pool.execute(
            """
            DELETE FROM gift_inventory
            WHERE owner_id=$1
            AND last_seen_at < now() - interval '1 hour' * $2
            AND id NOT IN (
                SELECT inventory_id FROM gift_transfer_items
                WHERE status = 'transferred'
            )
            """,
            owner_id,
            hours_old,
        )
        parts = result.split()
        return int(parts[2]) if len(parts) >= 3 else 0
