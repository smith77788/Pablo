from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def create_version(pool, contact_id: str, owner_id: int, snapshot: dict,
                         changed_fields: list = None, changed_by: str = 'sync',
                         change_summary: str = None) -> int:
    conn = await pool.acquire()
    try:
        async with conn.transaction():
            row = await conn.fetchval(
                'SELECT COALESCE(MAX(version_num), 0) FROM contact_versions WHERE contact_id=$1',
                contact_id)
            version_num = row + 1
            version_id = await conn.fetchval(
                '''INSERT INTO contact_versions (contact_id, owner_id, version_num, snapshot,
                    changed_fields, changed_by, change_summary)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id''',
                contact_id, owner_id, version_num, json.dumps(snapshot),
                changed_fields or [], changed_by, change_summary)
    finally:
        await pool.release(conn)
    return version_id


async def get_versions(pool, contact_id: str, owner_id: int, limit: int = 50) -> list:
    rows = await pool.fetch(
        '''SELECT id, version_num, changed_fields, changed_by, change_summary, created_at
           FROM contact_versions WHERE contact_id=$1 AND owner_id=$2
           ORDER BY version_num DESC LIMIT $3''',
        contact_id, owner_id, limit)
    return [dict(r) for r in rows]


async def get_version_detail(pool, version_id: int, owner_id: int) -> Optional[dict]:
    row = await pool.fetchrow(
        'SELECT * FROM contact_versions WHERE id=$1 AND owner_id=$2', version_id, owner_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get('snapshot'), str):
        d['snapshot'] = json.loads(d['snapshot'])
    return d


async def rollback_to_version(pool, contact_id: str, owner_id: int, version_num: int) -> bool:
    ver = await pool.fetchrow(
        'SELECT snapshot FROM contact_versions WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_num)
    if not ver:
        return False
    snapshot = ver['snapshot']
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    updatable = ['first_name', 'last_name', 'username', 'display_name', 'phones', 'emails',
                 'company', 'position', 'websites', 'addresses', 'birthday', 'notes', 'tags',
                 'color_label', 'is_favorite', 'is_premium', 'importance_level', 'user_rating',
                 'custom_fields']
    sets = []
    params = []
    idx = 1
    for field in updatable:
        if field in snapshot:
            val = snapshot[field]
            if field in ('phones', 'emails', 'websites', 'addresses', 'custom_fields'):
                if isinstance(val, str):
                    sets.append(f'{field} = ${idx}::jsonb')
                else:
                    sets.append(f'{field} = ${idx}::jsonb')
                    val = json.dumps(val)
            elif field == 'tags':
                sets.append(f'{field} = ${idx}::text[]')
            else:
                sets.append(f'{field} = ${idx}')
            params.append(val)
            idx += 1
    if not sets:
        return False
    sets.append('updated_at = NOW()')
    params.extend([contact_id, owner_id])
    await pool.execute(
        f'UPDATE unified_contacts SET {", ".join(sets)} WHERE id=${idx} AND owner_id=${idx+1}',
        *params)
    await create_version(pool, contact_id, owner_id, snapshot,
                         changed_by='rollback',
                         change_summary=f'Rollback to v{version_num}')
    return True


async def get_contact_snapshot(pool, contact_id: str) -> dict:
    row = await pool.fetchrow('SELECT * FROM unified_contacts WHERE id=$1', contact_id)
    if not row:
        return {}
    d = dict(row)
    for k in ('phones', 'emails', 'websites', 'addresses', 'custom_fields', 'digital_footprint'):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
