from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def build_identity_graph(pool, owner_id: int, contact_id: str) -> dict:
    contact = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2', contact_id, owner_id)
    if not contact:
        return {}
    c = dict(contact)

    identities = []
    if c.get('telegram_user_id'):
        identities.append({
            'type': 'telegram_id', 'value': str(c['telegram_user_id']),
            'is_primary': True, 'confidence': 1.0, 'source': 'telegram'})
    if c.get('username'):
        identities.append({
            'type': 'username', 'value': f"@{c['username']}",
            'is_primary': True, 'confidence': 1.0, 'source': 'telegram'})

    phones = c.get('phones', [])
    if isinstance(phones, str):
        try:
            phones = json.loads(phones)
        except (json.JSONDecodeError, TypeError):
            phones = []
    for p in phones:
        if p:
            identities.append({
                'type': 'phone', 'value': p,
                'is_primary': False, 'confidence': 1.0, 'source': 'contacts'})

    emails = c.get('emails', [])
    if isinstance(emails, str):
        try:
            emails = json.loads(emails)
        except (json.JSONDecodeError, TypeError):
            emails = []
    for e in emails:
        if e:
            identities.append({
                'type': 'email', 'value': e,
                'is_primary': False, 'confidence': 1.0, 'source': 'manual'})

    websites = c.get('websites', [])
    if isinstance(websites, str):
        try:
            websites = json.loads(websites)
        except (json.JSONDecodeError, TypeError):
            websites = []
    for w in websites:
        if w:
            identities.append({
                'type': 'website', 'value': w,
                'is_primary': False, 'confidence': 1.0, 'source': 'manual'})

    sources = await pool.fetch(
        'SELECT cs.*, ta.first_name as acc_name, ta.phone as acc_phone FROM contact_sources cs LEFT JOIN tg_accounts ta ON ta.id = cs.account_id WHERE cs.contact_id=$1',
        contact_id)
    for s in sources:
        identities.append({
            'type': 'tg_account', 'value': f"Account #{s['account_id']}",
            'is_primary': False, 'confidence': 1.0, 'source': 'sync',
            'metadata': {'account_name': s.get('acc_name'), 'local_name': s.get('local_name')}})

    await pool.execute(
        'DELETE FROM contact_identity_graph WHERE owner_id=$1 AND contact_id=$2', owner_id, contact_id)
    for ident in identities:
        meta = ident.pop('metadata', {})
        await pool.execute(
            '''INSERT INTO contact_identity_graph (owner_id, contact_id, identity_type, identity_value,
                is_primary, confidence, source)
               VALUES ($1,$2,$3,$4,$5,$6,$7)''',
            owner_id, contact_id, ident['type'], ident['value'],
            ident['is_primary'], ident['confidence'], ident['source'])

    return {
        'contact_id': contact_id,
        'identities': identities,
        'primary_count': sum(1 for i in identities if i.get('is_primary')),
        'total_count': len(identities),
    }


async def get_identity_graph(pool, owner_id: int, contact_id: str) -> list:
    rows = await pool.fetch(
        'SELECT * FROM contact_identity_graph WHERE owner_id=$1 AND contact_id=$2 ORDER BY is_primary DESC, identity_type',
        owner_id, contact_id)
    return [dict(r) for r in rows]


async def get_last_active(pool, contact_id: str) -> Optional[str]:
    row = await pool.fetchrow(
        'SELECT last_active_at FROM unified_contacts WHERE id=$1', contact_id)
    if row and row['last_active_at']:
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - row['last_active_at']
        if delta.days == 0:
            return 'Был сегодня'
        elif delta.days == 1:
            return 'Был вчера'
        elif delta.days < 7:
            return f'Был {delta.days} дн. назад'
        elif delta.days < 30:
            weeks = delta.days // 7
            return f'Был {weeks} нед. назад'
        elif delta.days < 365:
            months = delta.days // 30
            return f'Был {months} мес. назад'
        else:
            years = delta.days // 365
            return f'Был {years} г. назад'
    return None


async def add_identity(
    pool, owner_id: int, contact_id: str,
    identity_type: str, identity_value: str,
    is_primary: bool = False, confidence: float = 1.0, source: str = 'manual',
) -> dict:
    row = await pool.fetchrow(
        '''INSERT INTO contact_identity_graph
            (owner_id, contact_id, identity_type, identity_value, is_primary, confidence, source)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           RETURNING id''',
        owner_id, contact_id, identity_type, identity_value,
        is_primary, confidence, source)
    return {'id': row['id'], 'identity_type': identity_type, 'identity_value': identity_value}


async def remove_identity(pool, owner_id: int, contact_id: str, identity_type: str, identity_value: str) -> bool:
    result = await pool.execute(
        '''DELETE FROM contact_identity_graph
           WHERE owner_id=$1 AND contact_id=$2 AND identity_type=$3 AND identity_value=$4''',
        owner_id, contact_id, identity_type, identity_value)
    return result == 'DELETE 1'


async def compute_digital_footprint(pool, owner_id: int, contact_id: str) -> dict:
    contact = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2', contact_id, owner_id)
    if not contact:
        return {'score': 0, 'channels': [], 'sources_count': 0}
    c = dict(contact)
    channels = []
    if c.get('telegram_user_id'):
        channels.append('telegram')
    phones = c.get('phones', [])
    if isinstance(phones, str):
        try:
            phones = json.loads(phones)
        except (json.JSONDecodeError, TypeError):
            phones = []
    if phones:
        channels.append('phone')
    emails = c.get('emails', [])
    if isinstance(emails, str):
        try:
            emails = json.loads(emails)
        except (json.JSONDecodeError, TypeError):
            emails = []
    if emails:
        channels.append('email')
    websites = c.get('websites', [])
    if isinstance(websites, str):
        try:
            websites = json.loads(websites)
        except (json.JSONDecodeError, TypeError):
            websites = []
    if websites:
        channels.append('website')
    sources = await pool.fetch(
        'SELECT COUNT(*) as cnt FROM contact_sources WHERE contact_id=$1', contact_id)
    sources_count = sources[0]['cnt'] if sources else 0
    score = min(1.0, len(channels) * 0.25 + sources_count * 0.1)
    return {'score': round(score, 2), 'channels': channels, 'sources_count': sources_count}
