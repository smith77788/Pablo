from __future__ import annotations
import csv
import io
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def export_csv(pool, owner_id: int, contact_ids: list = None) -> str:
    if contact_ids:
        placeholders = ','.join([f'${i+2}' for i in range(len(contact_ids))])
        params = [owner_id] + contact_ids
        rows = await pool.fetch(
            f'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id IN ({placeholders}) ORDER BY first_name',
            *params)
    else:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name', owner_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Telegram User ID', 'Username', 'First Name', 'Last Name',
                     'Display Name', 'Phones', 'Emails', 'Company', 'Position',
                     'Websites', 'Birthday', 'Notes', 'Tags', 'Color Label',
                     'Is Favorite', 'Is Premium', 'Importance Level', 'User Rating',
                     'Discovered At', 'Last Synced At', 'Created At'])

    for r in rows:
        d = dict(r)
        phones = d.get('phones', [])
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except (json.JSONDecodeError, TypeError):
                phones = []
        emails = d.get('emails', [])
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except (json.JSONDecodeError, TypeError):
                emails = []
        websites = d.get('websites', [])
        if isinstance(websites, str):
            try:
                websites = json.loads(websites)
            except (json.JSONDecodeError, TypeError):
                websites = []
        tags = d.get('tags', [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.strip('{}').split(',') if t.strip()]

        writer.writerow([
            d.get('id', ''), d.get('telegram_user_id', ''), d.get('username', ''),
            d.get('first_name', ''), d.get('last_name', ''), d.get('display_name', ''),
            '; '.join(phones) if phones else '',
            '; '.join(emails) if emails else '',
            d.get('company', ''), d.get('position', ''),
            '; '.join(websites) if websites else '',
            d.get('birthday', ''), d.get('notes', ''),
            ', '.join(tags) if tags else '',
            d.get('color_label', ''),
            'Yes' if d.get('is_favorite') else 'No',
            'Yes' if d.get('is_premium') else 'No',
            d.get('importance_level', ''), d.get('user_rating', ''),
            d.get('discovered_at', ''), d.get('last_synced_at', ''),
            d.get('created_at', ''),
        ])

    return output.getvalue()


async def export_vcf(pool, owner_id: int, contact_ids: list = None) -> str:
    if contact_ids:
        placeholders = ','.join([f'${i+2}' for i in range(len(contact_ids))])
        params = [owner_id] + contact_ids
        rows = await pool.fetch(
            f'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id IN ({placeholders}) ORDER BY first_name',
            *params)
    else:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name', owner_id)

    lines = []
    for r in rows:
        d = dict(r)
        lines.append('BEGIN:VCARD')
        lines.append('VERSION:3.0')

        name = d.get('last_name', '') or ''
        first = d.get('first_name', '') or ''
        lines.append(f'N:{name};{first};;;')
        lines.append(f'FN:{first} {name}'.strip())

        if d.get('company'):
            lines.append(f'ORG:{d["company"]}')
        if d.get('position'):
            lines.append(f'TITLE:{d["position"]}')

        phones = d.get('phones', [])
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except (json.JSONDecodeError, TypeError):
                phones = []
        for p in phones:
            if p:
                lines.append(f'TEL;TYPE=CELL:{p}')

        emails = d.get('emails', [])
        if isinstance(emails, str):
            try:
                emails = json.loads(emails)
            except (json.JSONDecodeError, TypeError):
                emails = []
        for e in emails:
            if e:
                lines.append(f'EMAIL:{e}')

        if d.get('username'):
            lines.append(f'X-TELEGRAM:@{d["username"]}')
        if d.get('telegram_user_id'):
            lines.append(f'X-TELEGRAM-ID:{d["telegram_user_id"]}')
        if d.get('notes'):
            lines.append(f'NOTE:{d["notes"]}')

        lines.append('END:VCARD')

    return '\n'.join(lines)


async def export_json(pool, owner_id: int, contact_ids: list = None) -> str:
    if contact_ids:
        placeholders = ','.join([f'${i+2}' for i in range(len(contact_ids))])
        params = [owner_id] + contact_ids
        rows = await pool.fetch(
            f'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id IN ({placeholders}) ORDER BY first_name',
            *params)
    else:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name', owner_id)

    result = []
    for r in rows:
        d = dict(r)
        for k in ('phones', 'emails', 'websites', 'addresses', 'custom_fields', 'digital_footprint'):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        if 'created_at' in d and d['created_at']:
            d['created_at'] = str(d['created_at'])
        if 'updated_at' in d and d['updated_at']:
            d['updated_at'] = str(d['updated_at'])
        if 'discovered_at' in d and d['discovered_at']:
            d['discovered_at'] = str(d['discovered_at'])
        if 'last_synced_at' in d and d['last_synced_at']:
            d['last_synced_at'] = str(d['last_synced_at'])
        if 'last_changed_at' in d and d['last_changed_at']:
            d['last_changed_at'] = str(d['last_changed_at'])
        result.append(d)

    return json.dumps(result, ensure_ascii=False, indent=2)
