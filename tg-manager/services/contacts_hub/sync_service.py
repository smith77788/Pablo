from __future__ import annotations
import json
import logging
import time

log = logging.getLogger(__name__)


async def sync_account(pool, owner_id: int, account_id: int) -> dict:
    from database.db import get_account_for_telethon
    from services import account_manager
    from services.contacts_hub.repository import log_sync

    started = time.monotonic()
    acc = await get_account_for_telethon(pool, account_id, owner_id)
    if not acc or not acc.get('session_str'):
        return {'error': 'Session not found', 'synced': 0}

    try:
        # account_manager.get_contacts owns its own client connect/disconnect and
        # uses the real GetContactsRequest TL call — client.get_contacts() is not
        # a real Telethon method (this used to raise AttributeError on every sync).
        contacts = await account_manager.get_contacts(acc['session_str'], _acc=dict(acc))
        created = 0
        updated = 0
        for c in contacts:
            user_id = c['user_id']
            username = c.get('username') or None
            first_name = c.get('first_name') or ''
            last_name = c.get('last_name') or ''
            display_name = f"{first_name} {last_name}".strip()
            phones = [c['phone']] if c.get('phone') else []
            is_premium = bool(c.get('is_premium'))

            existing = await pool.fetchrow(
                'SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2',
                owner_id, user_id)
            if existing:
                await pool.execute(
                    'UPDATE unified_contacts SET username=$1, first_name=$2, last_name=$3, display_name=$4, '
                    'phones=$5::jsonb, is_premium=$6, last_synced_at=NOW(), updated_at=NOW() WHERE id=$7',
                    username, first_name, last_name, display_name,
                    json.dumps(phones), is_premium, existing['id'])
                updated += 1
            else:
                contact_id = str(__import__('uuid').uuid4())
                await pool.execute(
                    'INSERT INTO unified_contacts (id, owner_id, telegram_user_id, username, first_name, '
                    'last_name, display_name, phones, is_premium, last_synced_at) '
                    'VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,NOW())',
                    contact_id, owner_id, user_id, username, first_name,
                    last_name, display_name, json.dumps(phones), is_premium)
                await pool.execute(
                    'INSERT INTO contact_sources (contact_id, account_id, local_name, last_synced_at) '
                    'VALUES ($1,$2,$3,NOW()) ON CONFLICT DO NOTHING',
                    contact_id, account_id, display_name)
                created += 1

        duration_ms = int((time.monotonic() - started) * 1000)
        await log_sync(pool, owner_id, account_id, 'auto', len(contacts), created, updated, 0, duration_ms)
        return {'synced': len(contacts), 'created': created, 'updated': updated}
    except Exception as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning('contacts_hub sync_account failed acc=%s: %s', account_id, e)
        await log_sync(pool, owner_id, account_id, 'auto', 0, 0, 0, 0, duration_ms, str(e)[:200])
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
