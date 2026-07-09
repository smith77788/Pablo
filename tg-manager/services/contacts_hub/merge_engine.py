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


async def merge_contacts_improved(pool, primary_id: str, secondary_id: str, owner_id: int) -> dict:
    row = await pool.fetchrow(
        'SELECT id, first_name, last_name, username, phones, company FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        primary_id, owner_id)
    if not row:
        return {'status': 'error', 'error': 'contact_not_found'}

    sec_row = await pool.fetchrow(
        'SELECT id FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        secondary_id, owner_id)
    if not sec_row:
        return {'status': 'error', 'error': 'secondary_not_found'}

    conn = await pool.acquire()
    try:
        async with conn.transaction():
            await conn.execute('UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2', primary_id, secondary_id)
            await conn.execute('UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2', primary_id, secondary_id)
            await conn.execute('DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2', secondary_id, owner_id)
    finally:
        await pool.release(conn)

    log.info('Merged contact %s -> %s for owner %d', secondary_id, primary_id, owner_id)
    return {'status': 'merged', 'primary_id': primary_id, 'removed_id': secondary_id}


async def get_merge_preview(pool, contact_a_id: str, contact_b_id: str, owner_id: int) -> dict:
    row_a = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        contact_a_id, owner_id)
    row_b = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        contact_b_id, owner_id)

    if not row_a or not row_b:
        return {'error': 'contact_not_found'}

    a = dict(row_a)
    b = dict(row_b)

    phones_a = a.get('phones') or []
    phones_b = b.get('phones') or []
    merged_phones = list(set(phones_a + phones_b))

    emails_a = a.get('emails') or []
    emails_b = b.get('emails') or []
    merged_emails = list(set(emails_a + emails_b))

    merged = {
        'first_name': a.get('first_name') or b.get('first_name'),
        'last_name': a.get('last_name') or b.get('last_name'),
        'username': a.get('username') or b.get('username'),
        'phones': merged_phones,
        'emails': merged_emails,
        'company': a.get('company') or b.get('company'),
        'position': a.get('position') or b.get('position'),
        'notes': a.get('notes') or b.get('notes'),
    }

    return {
        'contact_a': {'id': contact_a_id, 'name': f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()},
        'contact_b': {'id': contact_b_id, 'name': f"{b.get('first_name', '')} {b.get('last_name', '')}".strip()},
        'merged_result': merged,
    }


async def auto_merge_with_confidence(pool, owner_id: int, threshold: float = 0.7) -> dict:
    duplicates = await find_duplicates(pool, owner_id)
    merged = 0
    skipped = 0

    for dup in duplicates:
        ids = dup['ids']
        primary = min(ids)

        for other_id in ids:
            if other_id == primary:
                continue

            confidence = 0.0
            primary_row = await pool.fetchrow(
                'SELECT telegram_user_id, username, first_name, last_name, phones, company FROM unified_contacts WHERE id=$1',
                primary)
            other_row = await pool.fetchrow(
                'SELECT telegram_user_id, username, first_name, last_name, phones, company FROM unified_contacts WHERE id=$1',
                other_id)

            if primary_row and other_row:
                a = dict(primary_row)
                b = dict(other_row)
                if a.get('telegram_user_id') and a['telegram_user_id'] == b.get('telegram_user_id'):
                    confidence = 1.0
                else:
                    phones_a = set(a.get('phones') or [])
                    phones_b = set(b.get('phones') or [])
                    if phones_a & phones_b:
                        confidence = 0.8
                    else:
                        confidence = 0.3

            if confidence >= threshold:
                conn = await pool.acquire()
                try:
                    async with conn.transaction():
                        await conn.execute('UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2', primary, other_id)
                        await conn.execute('UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2', primary, other_id)
                        await conn.execute('DELETE FROM unified_contacts WHERE id=$1', other_id)
                    merged += 1
                finally:
                    await pool.release(conn)
            else:
                skipped += 1

    return {'duplicates_found': len(duplicates), 'contacts_merged': merged, 'skipped': skipped}
