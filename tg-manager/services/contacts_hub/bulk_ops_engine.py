from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def bulk_tag(pool, owner_id: int, contact_ids: list, tag: str) -> dict:
    updated = 0
    for cid in contact_ids:
        result = await pool.execute(
            '''UPDATE unified_contacts SET tags = array_append(
                CASE WHEN $3 = ANY(tags) THEN tags ELSE tags END, $3),
               updated_at = NOW()
               WHERE id=$1 AND owner_id=$2 AND NOT ($3 = ANY(tags))''',
            cid, owner_id, tag)
        if result == 'UPDATE 1':
            updated += 1
    return {'updated': updated, 'tag': tag}


async def bulk_untag(pool, owner_id: int, contact_ids: list, tag: str) -> dict:
    updated = 0
    for cid in contact_ids:
        result = await pool.execute(
            'UPDATE unified_contacts SET tags = array_remove(tags, $3), updated_at=NOW() WHERE id=$1 AND owner_id=$2',
            cid, owner_id, tag)
        if result == 'UPDATE 1':
            updated += 1
    return {'updated': updated, 'tag': tag}


async def bulk_set_favorite(pool, owner_id: int, contact_ids: list, is_favorite: bool) -> dict:
    placeholders = ','.join([f'${i+3}' for i in range(len(contact_ids))])
    params = [is_favorite, owner_id] + contact_ids
    result = await pool.execute(
        f'UPDATE unified_contacts SET is_favorite=$1, updated_at=NOW() WHERE owner_id=$2 AND id IN ({placeholders})',
        *params)
    return {'updated': int(result.split()[-1]) if result.startswith('UPDATE') else 0}


async def bulk_delete(pool, owner_id: int, contact_ids: list) -> dict:
    placeholders = ','.join([f'${i+2}' for i in range(len(contact_ids))])
    params = [owner_id] + contact_ids
    result = await pool.execute(
        f'DELETE FROM unified_contacts WHERE owner_id=$1 AND id IN ({placeholders})', *params)
    return {'deleted': int(result.split()[-1]) if result.startswith('DELETE') else 0}


async def bulk_add_to_group(pool, owner_id: int, contact_ids: list, group_id: int) -> dict:
    added = 0
    for cid in contact_ids:
        try:
            await pool.execute(
                'INSERT INTO contact_group_members (group_id, contact_id) VALUES ($1,$2) ON CONFLICT DO NOTHING',
                group_id, cid)
            added += 1
        except Exception:
            pass
    return {'added': added}


async def bulk_remove_from_group(pool, owner_id: int, contact_ids: list, group_id: int) -> dict:
    removed = 0
    for cid in contact_ids:
        result = await pool.execute(
            'DELETE FROM contact_group_members WHERE group_id=$1 AND contact_id=$2', group_id, cid)
        if result == 'DELETE 1':
            removed += 1
    return {'removed': removed}


async def create_group(pool, owner_id: int, name: str, color: str = None) -> int:
    group_id = await pool.fetchval(
        'INSERT INTO contact_groups (owner_id, name, color) VALUES ($1,$2,$3) RETURNING id',
        owner_id, name, color)
    return group_id


async def update_group(pool, group_id: int, owner_id: int, name: str = None, color: str = None) -> bool:
    sets = []
    params = []
    idx = 1
    if name is not None:
        sets.append(f'name = ${idx}')
        params.append(name)
        idx += 1
    if color is not None:
        sets.append(f'color = ${idx}')
        params.append(color)
        idx += 1
    if not sets:
        return False
    params.extend([group_id, owner_id])
    result = await pool.execute(
        f'UPDATE contact_groups SET {", ".join(sets)} WHERE id=${idx} AND owner_id=${idx+1}',
        *params)
    return result != 'UPDATE 0'


async def delete_group(pool, group_id: int, owner_id: int) -> bool:
    await pool.execute(
        'DELETE FROM contact_group_members WHERE group_id=$1', group_id)
    result = await pool.execute(
        'DELETE FROM contact_groups WHERE id=$1 AND owner_id=$2', group_id, owner_id)
    return result != 'DELETE 0'


async def bulk_merge(pool, owner_id: int, pairs: list) -> dict:
    merged = 0
    conn = await pool.acquire()
    try:
        for pair in pairs:
            primary_id = pair.get('primary_id')
            secondary_id = pair.get('secondary_id')
            if not primary_id or not secondary_id or primary_id == secondary_id:
                continue
            async with conn.transaction():
                await conn.execute(
                    'UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2',
                    primary_id, secondary_id)
                await conn.execute(
                    'UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2',
                    primary_id, secondary_id)
                await conn.execute(
                    'DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2',
                    secondary_id, owner_id)
            merged += 1
    finally:
        await pool.release(conn)
    return {'merged': merged}


async def bulk_export(pool, owner_id: int, contact_ids: list, format: str = 'json') -> dict:
    if format == 'csv':
        from services.contacts_hub.export_engine import export_csv
        data = await export_csv(pool, owner_id, contact_ids)
        return {'format': 'csv', 'data': data}
    if format == 'vcf':
        from services.contacts_hub.export_engine import export_vcf
        data = await export_vcf(pool, owner_id, contact_ids)
        return {'format': 'vcf', 'data': data}
    from services.contacts_hub.export_engine import export_json
    data = await export_json(pool, owner_id, contact_ids)
    return {'format': 'json', 'data': data}


async def bulk_add_tags(pool, owner_id: int, contact_ids: list, tags: list) -> dict:
    updated = 0
    for cid in contact_ids:
        for tag in tags:
            result = await pool.execute(
                '''UPDATE unified_contacts SET tags = array_append(
                    CASE WHEN $3 = ANY(tags) THEN tags ELSE tags END, $3),
                   updated_at = NOW()
                   WHERE id=$1 AND owner_id=$2 AND NOT ($3 = ANY(tags))''',
                cid, owner_id, tag)
            if result == 'UPDATE 1':
                updated += 1
    return {'updated': updated}


async def bulk_remove_tags(pool, owner_id: int, contact_ids: list, tags: list) -> dict:
    updated = 0
    for cid in contact_ids:
        for tag in tags:
            result = await pool.execute(
                'UPDATE unified_contacts SET tags = array_remove(tags, $3), updated_at=NOW() WHERE id=$1 AND owner_id=$2',
                cid, owner_id, tag)
            if result == 'UPDATE 1':
                updated += 1
    return {'updated': updated}


async def bulk_set_importance(pool, owner_id: int, contact_ids: list, level: int) -> dict:
    placeholders = ','.join([f'${i+3}' for i in range(len(contact_ids))])
    params = [level, owner_id] + contact_ids
    result = await pool.execute(
        f'UPDATE unified_contacts SET importance_level=$1, updated_at=NOW() WHERE owner_id=$2 AND id IN ({placeholders})',
        *params)
    return {'updated': int(result.split()[-1]) if result.startswith('UPDATE') else 0}


async def bulk_set_rating(pool, owner_id: int, contact_ids: list, rating: float) -> dict:
    placeholders = ','.join([f'${i+3}' for i in range(len(contact_ids))])
    params = [rating, owner_id] + contact_ids
    result = await pool.execute(
        f'UPDATE unified_contacts SET user_rating=$1, updated_at=NOW() WHERE owner_id=$2 AND id IN ({placeholders})',
        *params)
    return {'updated': int(result.split()[-1]) if result.startswith('UPDATE') else 0}
