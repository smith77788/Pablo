from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


def compute_trust_score(contact_data: dict, sources: list) -> float:
    score = 0.5
    if contact_data.get('telegram_user_id'):
        score += 0.2
    if contact_data.get('username'):
        score += 0.1
    if contact_data.get('phones') and len(contact_data['phones']) > 0:
        score += 0.1
    if len(sources) > 1:
        score += 0.1
    if len(sources) >= 3:
        score += 0.05
    return min(score, 1.0)


def compute_merge_confidence(contact_a: dict, contact_b: dict) -> dict:
    score = 0.0
    reasons = []

    if contact_a.get('telegram_user_id') and contact_a['telegram_user_id'] == contact_b.get('telegram_user_id'):
        score += 0.5
        reasons.append('same_telegram_id')

    phones_a = set(contact_a.get('phones') or [])
    phones_b = set(contact_b.get('phones') or [])
    if phones_a and phones_b and phones_a & phones_b:
        score += 0.3
        reasons.append('same_phone')

    username_a = (contact_a.get('username') or '').lower()
    username_b = (contact_b.get('username') or '').lower()
    if username_a and username_b and username_a == username_b:
        score += 0.25
        reasons.append('same_username')

    name_a = f"{(contact_a.get('first_name') or '').lower()} {(contact_a.get('last_name') or '').lower()}"
    name_b = f"{(contact_b.get('first_name') or '').lower()} {(contact_b.get('last_name') or '').lower()}"
    if name_a.strip() and name_b.strip():
        if name_a == name_b:
            score += 0.15
            reasons.append('same_name')
        elif name_a.split()[0] == name_b.split()[0]:
            score += 0.05
            reasons.append('same_first_name')

    company_a = (contact_a.get('company') or '').lower()
    company_b = (contact_b.get('company') or '').lower()
    if company_a and company_b and company_a == company_b:
        score += 0.1
        reasons.append('same_company')

    return {'confidence': min(score, 1.0), 'reasons': reasons}


async def detect_smart_duplicates(pool, owner_id: int) -> list:
    contacts = await pool.fetch(
        '''SELECT id, telegram_user_id, username, first_name, last_name, phones, company
           FROM unified_contacts WHERE owner_id=$1''', owner_id)
    duplicates = []
    seen_pairs = set()

    for i in range(len(contacts)):
        for j in range(i+1, len(contacts)):
            a = dict(contacts[i])
            b = dict(contacts[j])
            key = tuple(sorted([a['id'], b['id']]))
            if key in seen_pairs:
                continue
            result = compute_merge_confidence(a, b)
            if result['confidence'] >= 0.5:
                seen_pairs.add(key)
                duplicates.append({
                    'contact_a': {'id': a['id'], 'name': f"{a.get('first_name', '')} {a.get('last_name', '')}".strip() or a.get('username', '?')},
                    'contact_b': {'id': b['id'], 'name': f"{b.get('first_name', '')} {b.get('last_name', '')}".strip() or b.get('username', '?')},
                    'confidence': result['confidence'],
                    'reasons': result['reasons'],
                })

    duplicates.sort(key=lambda x: x['confidence'], reverse=True)
    return duplicates


async def update_trust_scores(pool, owner_id: int) -> int:
    contacts = await pool.fetch(
        'SELECT id FROM unified_contacts WHERE owner_id=$1', owner_id)
    updated = 0
    for c in contacts:
        sources = await pool.fetch(
            'SELECT * FROM contact_sources WHERE contact_id=$1', c['id'])
        contact = await pool.fetchrow(
            'SELECT * FROM unified_contacts WHERE id=$1', c['id'])
        if contact:
            score = compute_trust_score(dict(contact), [dict(s) for s in sources])
            await pool.execute(
                'UPDATE unified_contacts SET trust_score=$1 WHERE id=$2', score, c['id'])
            updated += 1
    return updated


async def get_conflicts(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT c.*, uc.first_name, uc.last_name, uc.username
           FROM contact_conflicts c
           JOIN unified_contacts uc ON uc.id = c.contact_id
           WHERE c.owner_id=$1 AND c.resolution='pending'
           ORDER BY c.created_at DESC''', owner_id)
    return [dict(r) for r in rows]


async def resolve_conflict(pool, conflict_id: int, owner_id: int,
                           resolution: str, resolved_value: str = None,
                           resolved_by: int = None) -> bool:
    result = await pool.execute(
        '''UPDATE contact_conflicts SET resolution=$1, resolved_value=$2,
           resolved_by=$3, resolved_at=NOW()
           WHERE id=$4 AND owner_id=$5''',
        resolution, resolved_value, resolved_by, conflict_id, owner_id)
    return result != 'UPDATE 0'


async def compute_merge_confidence_improved(pool, owner_id: int, contact_a_id: int, contact_b_id: int) -> dict:
    """НЕ ИСПОЛЬЗУЕТСЯ: продукт зовёт `detect_smart_duplicates` /
    `compute_merge_confidence` (без суффикса). Эта «улучшенная» ветка —
    параллельная реализация, на которую нет ни одной ссылки, кроме тестов.
    Прежде чем развивать её, решите, какая из двух остаётся единственной:
    две реализации одного правила расходятся молча."""
    row_a = await pool.fetchrow(
        '''SELECT id, telegram_user_id, username, first_name, last_name, phones, company
           FROM unified_contacts WHERE id=$1 AND owner_id=$2''', contact_a_id, owner_id)
    row_b = await pool.fetchrow(
        '''SELECT id, telegram_user_id, username, first_name, last_name, phones, company
           FROM unified_contacts WHERE id=$1 AND owner_id=$2''', contact_b_id, owner_id)
    if not row_a or not row_b:
        return {'confidence': 0.0, 'reasons': [], 'error': 'contact_not_found'}

    a = dict(row_a)
    b = dict(row_b)
    base = compute_merge_confidence(a, b)

    # Колонки source_type в contact_sources нет: этот запрос упал бы при первом
    # же вызове (см. пометку в докстринге — ветка не подключена). Источник
    # контакта в этой схеме — АККАУНТ, через который его увидели (account_id).
    # Совпадение аккаунтов — тот же сигнал: одного человека видно с обеих сторон.
    sources_a = await pool.fetch(
        'SELECT account_id FROM contact_sources WHERE contact_id=$1', contact_a_id)
    sources_b = await pool.fetch(
        'SELECT account_id FROM contact_sources WHERE contact_id=$1', contact_b_id)
    types_a = {s['account_id'] for s in sources_a if s['account_id'] is not None}
    types_b = {s['account_id'] for s in sources_b if s['account_id'] is not None}
    overlap = types_a & types_b
    if len(overlap) >= 2:
        base['confidence'] = min(base['confidence'] + 0.1, 1.0)
        base['reasons'].append('overlapping_sources')

    if a.get('telegram_user_id') and not b.get('telegram_user_id'):
        base['confidence'] = min(base['confidence'] + 0.05, 1.0)
        base['reasons'].append('telegram_id_transfer')
    elif b.get('telegram_user_id') and not a.get('telegram_user_id'):
        base['confidence'] = min(base['confidence'] + 0.05, 1.0)
        base['reasons'].append('telegram_id_transfer')

    name_a = f"{(a.get('first_name') or '').lower()} {(a.get('last_name') or '').lower()}".strip()
    name_b = f"{(b.get('first_name') or '').lower()} {(b.get('last_name') or '').lower()}".strip()
    if name_a and name_b and name_a != name_b:
        parts_a = set(name_a.split())
        parts_b = set(name_b.split())
        shared = parts_a & parts_b
        if shared and len(shared) >= 2:
            base['confidence'] = min(base['confidence'] + 0.05, 1.0)
            base['reasons'].append('partial_name_overlap')

    return base


async def detect_smart_duplicates_improved(pool, owner_id: int) -> list:
    """НЕ ИСПОЛЬЗУЕТСЯ: продукт зовёт `detect_smart_duplicates` /
    `compute_merge_confidence` (без суффикса). Эта «улучшенная» ветка —
    параллельная реализация, на которую нет ни одной ссылки, кроме тестов.
    Прежде чем развивать её, решите, какая из двух остаётся единственной:
    две реализации одного правила расходятся молча."""
    contacts = await pool.fetch(
        '''SELECT id, telegram_user_id, username, first_name, last_name, phones, company
           FROM unified_contacts WHERE owner_id=$1''', owner_id)
    if len(contacts) < 2:
        return []

    source_map = {}
    for c in contacts:
        # См. compute_merge_confidence_improved: колонки source_type нет,
        # источник контакта — аккаунт, через который его увидели.
        sources = await pool.fetch(
            'SELECT account_id FROM contact_sources WHERE contact_id=$1', c['id'])
        source_map[c['id']] = {s['account_id'] for s in sources
                               if s['account_id'] is not None}

    duplicates = []
    seen_pairs = set()

    for i in range(len(contacts)):
        for j in range(i + 1, len(contacts)):
            a = dict(contacts[i])
            b = dict(contacts[j])
            key = tuple(sorted([a['id'], b['id']]))
            if key in seen_pairs:
                continue

            base = compute_merge_confidence(a, b)

            types_a = source_map.get(a['id'], set())
            types_b = source_map.get(b['id'], set())
            overlap = types_a & types_b
            if len(overlap) >= 2:
                base['confidence'] = min(base['confidence'] + 0.1, 1.0)
                base['reasons'].append('overlapping_sources')

            if base['confidence'] >= 0.45:
                seen_pairs.add(key)
                name_a = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip() or a.get('username', '?')
                name_b = f"{b.get('first_name', '')} {b.get('last_name', '')}".strip() or b.get('username', '?')
                duplicates.append({
                    'contact_a': {'id': a['id'], 'name': name_a},
                    'contact_b': {'id': b['id'], 'name': name_b},
                    'confidence': round(base['confidence'], 3),
                    'reasons': base['reasons'],
                    'source_overlap': list(overlap),
                })

    duplicates.sort(key=lambda x: x['confidence'], reverse=True)
    return duplicates


async def get_conflict_resolution_suggestions(pool, conflict_id: int) -> dict:
    row = await pool.fetchrow(
        '''SELECT c.*, uc.first_name, uc.last_name, uc.username, uc.phones, uc.company,
                  uc.telegram_user_id
           FROM contact_conflicts c
           JOIN unified_contacts uc ON uc.id = c.contact_id
           WHERE c.id=$1''', conflict_id)
    if not row:
        return {'error': 'conflict_not_found'}

    conflict = dict(row)
    field = conflict.get('conflict_field') or conflict.get('field')
    suggestions = []

    if field == 'phones':
        phones = conflict.get('phones') or []
        if len(phones) >= 2:
            suggestions.append({'action': 'merge_phones', 'values': phones, 'rationale': 'all_phones_valid'})
        elif phones:
            suggestions.append({'action': 'keep_value', 'value': phones[0], 'rationale': 'single_phone'})
    elif field == 'name' or field == 'first_name':
        suggestions.append({
            'action': 'keep_most_recent',
            'value': conflict.get('first_name'),
            'rationale': 'latest_source_wins',
        })
    elif field == 'username':
        suggestions.append({
            'action': 'keep_value',
            'value': conflict.get('username'),
            'rationale': 'username_unique',
        })
    elif field == 'company':
        suggestions.append({
            'action': 'keep_value',
            'value': conflict.get('company'),
            'rationale': 'company_singular',
        })
    else:
        suggestions.append({
            'action': 'manual_review',
            'value': conflict.get('resolved_value'),
            'rationale': 'unknown_field_type',
        })

    priority = 'low'
    if conflict.get('confidence', 0) >= 0.8:
        priority = 'high'
    elif conflict.get('confidence', 0) >= 0.6:
        priority = 'medium'

    return {
        'conflict_id': conflict_id,
        'field': field,
        'priority': priority,
        'suggestions': suggestions,
        'current_resolution': conflict.get('resolution'),
    }
