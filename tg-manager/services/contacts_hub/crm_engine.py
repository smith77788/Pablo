from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def get_crm_data(pool, owner_id: int, contact_id: str) -> Optional[dict]:
    row = await pool.fetchrow(
        'SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2',
        owner_id, contact_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get('custom_fields'), str):
        try:
            d['custom_fields'] = json.loads(d['custom_fields'])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


async def upsert_crm(pool, owner_id: int, contact_id: str, data: dict) -> dict:
    existing = await get_crm_data(pool, owner_id, contact_id)
    if existing:
        sets = []
        params = []
        idx = 1
        for field in ('stage', 'deal_value', 'currency', 'last_interaction_at',
                      'last_interaction_type', 'last_message_preview',
                      'next_reminder_at', 'next_reminder_text', 'assigned_to'):
            if field in data:
                sets.append(f'{field} = ${idx}')
                params.append(data[field])
                idx += 1
        if 'custom_fields' in data:
            sets.append(f'custom_fields = ${idx}::jsonb')
            params.append(json.dumps(data['custom_fields']))
            idx += 1
        if sets:
            sets.append('updated_at = NOW()')
            params.extend([owner_id, contact_id])
            await pool.execute(
                f'UPDATE contact_crm SET {", ".join(sets)} WHERE owner_id=${idx} AND contact_id=${idx+1}',
                *params)
    else:
        await pool.execute(
            '''INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency,
                next_reminder_at, next_reminder_text, custom_fields)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)''',
            owner_id, contact_id,
            data.get('stage', 'lead'),
            data.get('deal_value', 0),
            data.get('currency', 'USD'),
            data.get('next_reminder_at'),
            data.get('next_reminder_text'),
            json.dumps(data.get('custom_fields', {})))
    return await get_crm_data(pool, owner_id, contact_id)


async def log_crm_activity(pool, owner_id: int, contact_id: str,
                           activity_type: str, description: str = None,
                           metadata: dict = None) -> None:
    await pool.execute(
        '''INSERT INTO contact_crm_activity (owner_id, contact_id, activity_type, description, metadata)
           VALUES ($1,$2,$3,$4,$5::jsonb)''',
        owner_id, contact_id, activity_type, description,
        json.dumps(metadata or {}))


async def get_crm_activity(pool, owner_id: int, contact_id: str, limit: int = 30) -> list:
    rows = await pool.fetch(
        '''SELECT * FROM contact_crm_activity
           WHERE owner_id=$1 AND contact_id=$2
           ORDER BY created_at DESC LIMIT $3''',
        owner_id, contact_id, limit)
    return [dict(r) for r in rows]


async def get_upcoming_reminders(pool, owner_id: int, limit: int = 20) -> list:
    rows = await pool.fetch(
        '''SELECT crm.*, uc.first_name, uc.last_name, uc.username
           FROM contact_crm crm
           JOIN unified_contacts uc ON uc.id = crm.contact_id
           WHERE crm.owner_id=$1 AND crm.next_reminder_at IS NOT NULL
           AND crm.next_reminder_at >= NOW()
           ORDER BY crm.next_reminder_at ASC LIMIT $2''',
        owner_id, limit)
    result = []
    for r in rows:
        d = dict(r)
        d['contact_name'] = f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip() or d.pop('username', '?')
        result.append(d)
    return result


async def get_crm_stats(pool, owner_id: int) -> dict:
    total = await pool.fetchval(
        'SELECT COUNT(*) FROM contact_crm WHERE owner_id=$1', owner_id)
    by_stage = await pool.fetch(
        '''SELECT stage, COUNT(*) as cnt, COALESCE(SUM(deal_value), 0) as total_value
           FROM contact_crm WHERE owner_id=$1 GROUP BY stage ORDER BY cnt DESC''',
        owner_id)
    reminders = await pool.fetchval(
        '''SELECT COUNT(*) FROM contact_crm
           WHERE owner_id=$1 AND next_reminder_at IS NOT NULL AND next_reminder_at <= NOW() + INTERVAL '7 days' ''',
        owner_id)
    return {
        'total_crm_contacts': total,
        'by_stage': [dict(r) for r in by_stage],
        'upcoming_reminders_7d': reminders,
    }


async def get_crm_overdue(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT crm.*, uc.first_name, uc.last_name, uc.username
           FROM contact_crm crm
           JOIN unified_contacts uc ON uc.id = crm.contact_id
           WHERE crm.owner_id=$1 AND crm.next_reminder_at < NOW()
           ORDER BY crm.next_reminder_at ASC''',
        owner_id)
    result = []
    for r in rows:
        d = dict(r)
        d['contact_name'] = f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip() or d.pop('username', '?')
        result.append(d)
    return result
