from __future__ import annotations
import asyncio
import json
import logging
import time

import asyncpg

log = logging.getLogger(__name__)


async def sync_account(pool, owner_id: int, account_id: int) -> dict:
    from services.account_manager import _make_client, get_account_for_telethon
    acc = await get_account_for_telethon(pool, account_id)
    if not acc or not acc.get('session_str'):
        return {'error': 'Session not found', 'synced': 0}
    client = _make_client(acc['session_str'])
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        contacts = await client.get_contacts()
        created = 0
        updated = 0
        for c in contacts:
            existing = await pool.fetchrow(
                'SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2',
                owner_id, c.id)
            data = {
                'telegram_user_id': c.id,
                'username': c.username,
                'first_name': c.first_name or '',
                'last_name': c.last_name or '',
                'display_name': f"{c.first_name or ''} {c.last_name or ''}".strip(),
                'phones': [c.phone] if c.phone else [],
                'is_premium': getattr(c, 'premium', False),
                'discovered_at': c.date if hasattr(c, 'date') else None,
                'last_synced_at': int(time.time()),
            }
            if existing:
                await pool.execute(
                    'UPDATE unified_contacts SET username=$1, first_name=$2, last_name=$3, display_name=$4, phones=$5::jsonb, is_premium=$6, last_synced_at=$7, updated_at=NOW() WHERE id=$8',
                    data['username'], data['first_name'], data['last_name'], data['display_name'],
                    json.dumps(data['phones']), data['is_premium'], data['last_synced_at'], existing['id'])
                updated += 1
            else:
                contact_id = str(__import__('uuid').uuid4())
                await pool.execute(
                    'INSERT INTO unified_contacts (id, owner_id, telegram_user_id, username, first_name, last_name, display_name, phones, is_premium, discovered_at, last_synced_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)',
                    contact_id, owner_id, data['telegram_user_id'], data['username'], data['first_name'],
                    data['last_name'], data['display_name'], json.dumps(data['phones']), data['is_premium'],
                    data['discovered_at'], data['last_synced_at'])
                await pool.execute(
                    'INSERT INTO contact_sources (contact_id, account_id, local_name, last_synced_at) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING',
                    contact_id, account_id, data['display_name'], data['last_synced_at'])
                created += 1
        await client.disconnect()
        duration_ms = int((time.time() - time.time()) * 1000)
        from services.contacts_hub.repository import log_sync
        await log_sync(pool, owner_id, account_id, 'auto', len(contacts), created, updated, 0, 0)
        return {'synced': len(contacts), 'created': created, 'updated': updated}
    except Exception as e:
        from services.contacts_hub.repository import log_sync
        await log_sync(pool, owner_id, account_id, 'auto', 0, 0, 0, 0, 0, str(e)[:200])
        return {'error': str(e)[:200], 'synced': 0}


async def sync_all_accounts(pool, owner_id: int) -> dict:
    accounts = await pool.fetch('SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL', owner_id)
    results = []
    for acc in accounts:
        result = await sync_account(pool, owner_id, acc['id'])
        results.append(result)
    total_synced = sum(r.get('synced', 0) for r in results)
    total_created = sum(r.get('created', 0) for r in results)
    total_updated = sum(r.get('updated', 0) for r in results)
    errors = [r.get('error') for r in results if r.get('error')]
    return {
        'accounts_synced': len(results),
        'total_synced': total_synced,
        'total_created': total_created,
        'total_updated': total_updated,
        'errors': errors,
    }
