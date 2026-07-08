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
