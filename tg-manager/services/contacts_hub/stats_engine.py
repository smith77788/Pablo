from __future__ import annotations
import logging

import asyncpg

log = logging.getLogger(__name__)


async def get_full_stats(pool, owner_id: int) -> dict:
    from services.contacts_hub.repository import get_contact_stats
    return await get_contact_stats(pool, owner_id)


async def get_account_stats(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT cs.account_id, COUNT(DISTINCT cs.contact_id) as contact_count,
                  ta.first_name as account_name, ta.phone as account_phone
           FROM contact_sources cs
           JOIN unified_contacts uc ON cs.contact_id = uc.id
           LEFT JOIN tg_accounts ta ON ta.id = cs.account_id
           WHERE uc.owner_id = $1
           GROUP BY cs.account_id, ta.first_name, ta.phone
           ORDER BY contact_count DESC''',
        owner_id)
    return [dict(r) for r in rows]
