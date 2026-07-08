from __future__ import annotations
import logging

import asyncpg

log = logging.getLogger(__name__)


async def find_duplicates(pool, owner_id) -> list:
    rows = await pool.fetch(
        'SELECT telegram_user_id, array_agg(id) as ids, COUNT(*) as cnt FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id IS NOT NULL GROUP BY telegram_user_id HAVING COUNT(*) > 1',
        owner_id)
    return [{'telegram_user_id': r['telegram_user_id'], 'ids': r['ids'], 'count': r['cnt']} for r in rows]


async def auto_merge(pool, owner_id) -> dict:
    duplicates = await find_duplicates(pool, owner_id)
    merged = 0
    conn = await pool.acquire()
    try:
        for dup in duplicates:
            ids = dup['ids']
            primary = min(ids)
            others = [i for i in ids if i != primary]
            for other_id in others:
                async with conn.transaction():
                    await conn.execute('UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2', primary, other_id)
                    await conn.execute('UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2', primary, other_id)
                    await conn.execute('DELETE FROM unified_contacts WHERE id=$1', other_id)
                merged += 1
    finally:
        await pool.release(conn)
    return {'duplicates_found': len(duplicates), 'contacts_merged': merged}


async def manual_merge(pool, primary_id: str, secondary_id: str, owner_id: int) -> dict:
    conn = await pool.acquire()
    try:
        async with conn.transaction():
            await conn.execute('UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2', primary_id, secondary_id)
            await conn.execute('UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2', primary_id, secondary_id)
            await conn.execute('DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2', secondary_id, owner_id)
    finally:
        await pool.release(conn)
    return {'status': 'merged', 'primary_id': primary_id, 'removed_id': secondary_id}
