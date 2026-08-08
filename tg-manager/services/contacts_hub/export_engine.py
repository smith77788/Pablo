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


async def export_vcard(pool, owner_id: int, contact_id: int) -> str:
    row = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id=$2',
        owner_id, contact_id)
    if not row:
        return ''

    d = dict(row)
    lines = ['BEGIN:VCARD', 'VERSION:3.0']

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

    websites = d.get('websites', [])
    if isinstance(websites, str):
        try:
            websites = json.loads(websites)
        except (json.JSONDecodeError, TypeError):
            websites = []
    for w in websites:
        if w:
            lines.append(f'URL:{w}')

    if d.get('username'):
        lines.append(f'X-TELEGRAM:@{d["username"]}')
    if d.get('telegram_user_id'):
        lines.append(f'X-TELEGRAM-ID:{d["telegram_user_id"]}')
    if d.get('birthday'):
        lines.append(f'BDAY:{d["birthday"]}')
    if d.get('notes'):
        lines.append(f'NOTE:{d["notes"]}')

    lines.append('END:VCARD')
    return '\n'.join(lines)


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


async def export_csv_streaming(pool, owner_id: int, contact_ids: list = None):
    header = ['ID', 'Telegram User ID', 'Username', 'First Name', 'Last Name',
              'Display Name', 'Phones', 'Emails', 'Company', 'Position',
              'Websites', 'Birthday', 'Notes', 'Tags', 'Color Label',
              'Is Favorite', 'Is Premium', 'Importance Level', 'User Rating',
              'Discovered At', 'Last Synced At', 'Created At']
    yield header

    if contact_ids:
        batch_size = 500
        for i in range(0, len(contact_ids), batch_size):
            batch = contact_ids[i:i + batch_size]
            placeholders = ','.join([f'${j+2}' for j in range(len(batch))])
            params = [owner_id] + batch
            rows = await pool.fetch(
                f'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id IN ({placeholders}) ORDER BY first_name',
                *params)
            for r in rows:
                yield _contact_to_csv_row(r)
    else:
        # Keyset-пагинация ДОЛЖНА сортировать по той же колонке, что и фильтр
        # курсора (id). Раньше было ORDER BY first_name при WHERE id > cursor —
        # колонки не совпадали: курсор-id не монотонен в порядке first_name,
        # из-за чего пагинация могла пропускать строки или зацикливаться
        # (last-row id не растёт → те же строки перечитываются бесконечно).
        cursor = None
        while True:
            if cursor:
                rows = await pool.fetch(
                    'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id > $2 ORDER BY id LIMIT 500',
                    owner_id, cursor)
            else:
                rows = await pool.fetch(
                    'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY id LIMIT 500',
                    owner_id)
            if not rows:
                break
            for r in rows:
                yield _contact_to_csv_row(r)
            cursor = dict(rows[-1])['id']


def _contact_to_csv_row(record) -> list:
    d = dict(record)
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

    return [
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
    ]


async def get_export_stats(pool, owner_id: int) -> dict:
    total = await pool.fetchval(
        'SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1', owner_id)
    with_phone = await pool.fetchval(
        "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND phones IS NOT NULL AND phones != '[]' AND phones != ''",
        owner_id)
    with_email = await pool.fetchval(
        "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND emails IS NOT NULL AND emails != '[]' AND emails != ''",
        owner_id)
    favorites = await pool.fetchval(
        'SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_favorite=true',
        owner_id)
    premium = await pool.fetchval(
        'SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_premium=true',
        owner_id)
    last_sync = await pool.fetchval(
        'SELECT MAX(last_synced_at) FROM unified_contacts WHERE owner_id=$1',
        owner_id)

    return {
        'total_contacts': total or 0,
        'with_phone': with_phone or 0,
        'with_email': with_email or 0,
        'favorites': favorites or 0,
        'premium': premium or 0,
        'last_synced': str(last_sync) if last_sync else None,
    }
