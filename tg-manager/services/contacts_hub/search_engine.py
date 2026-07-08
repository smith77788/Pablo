from __future__ import annotations
import logging

import asyncpg

log = logging.getLogger(__name__)


async def search_contacts(pool, owner_id: int, query: str, limit: int = 50) -> list:
    q = f'%{query}%'
    rows = await pool.fetch(
        '''SELECT * FROM unified_contacts
           WHERE owner_id = $1 AND (
               first_name ILIKE $2 OR last_name ILIKE $2 OR
               username ILIKE $2 OR display_name ILIKE $2 OR
               CAST(telegram_user_id AS TEXT) ILIKE $2 OR
               company ILIKE $2 OR notes ILIKE $2 OR
               $2 = ANY(tags)
           )
           ORDER BY
               CASE WHEN first_name ILIKE $2 THEN 0
                    WHEN username ILIKE $2 THEN 1
                    WHEN display_name ILIKE $2 THEN 2
                    ELSE 3 END,
               first_name
           LIMIT $3''',
        owner_id, q, limit)
    return [dict(r) for r in rows]
