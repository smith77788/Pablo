from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

RELATIONSHIP_TYPES = {
    'mutual_group': 'Общая группа',
    'mutual_channel': 'Общий канал',
    'mutual_account': 'Общий аккаунт',
    'same_company': 'Одна компания',
    'same_username_pattern': 'Похожий username',
    'same_phone_domain': 'Одинаковый домен телефона',
    'phone_match': 'Совпадение телефона',
}


async def compute_relationships(pool, owner_id: int, contact_id: str = None) -> dict:
    if contact_id:
        contacts = await pool.fetch(
            'SELECT id, telegram_user_id, username, phones, company FROM unified_contacts WHERE owner_id=$1 AND id=$2',
            owner_id, contact_id)
    else:
        contacts = await pool.fetch(
            'SELECT id, telegram_user_id, username, phones, company FROM unified_contacts WHERE owner_id=$1',
            owner_id)
    if len(contacts) < 2:
        return {'relationships': [], 'count': 0}

    phone_map = {}
    username_map = {}
    company_map = {}
    for c in contacts:
        cid = c['id']
        phones = c['phones'] or []
        if isinstance(phones, str):
            import json
            try:
                phones = json.loads(phones)
            except (json.JSONDecodeError, TypeError):
                phones = []
        for p in phones:
            if p:
                phone_map.setdefault(p, []).append(cid)
        if c['username']:
            username_map.setdefault(c['username'].lower(), []).append(cid)
        if c['company']:
            company_map.setdefault(c['company'].lower(), []).append(cid)

    account_map = {}
    sources = await pool.fetch(
        'SELECT contact_id, account_id FROM contact_sources WHERE contact_id IN (SELECT id FROM unified_contacts WHERE owner_id=$1)',
        owner_id)
    for s in sources:
        account_map.setdefault(s['account_id'], set()).add(s['contact_id'])

    relationships = []
    seen = set()

    for phone, cids in phone_map.items():
        if len(cids) > 1:
            for i in range(len(cids)):
                for j in range(i+1, len(cids)):
                    key = tuple(sorted([cids[i], cids[j]]))
                    if key not in seen:
                        seen.add(key)
                        relationships.append({
                            'contact_a_id': cids[i], 'contact_b_id': cids[j],
                            'relationship_type': 'phone_match', 'strength': 0.95,
                            'metadata': {'phone': phone}})

    for uname, cids in username_map.items():
        if len(cids) > 1:
            for i in range(len(cids)):
                for j in range(i+1, len(cids)):
                    key = tuple(sorted([cids[i], cids[j]]))
                    if key not in seen:
                        seen.add(key)
                        relationships.append({
                            'contact_a_id': cids[i], 'contact_b_id': cids[j],
                            'relationship_type': 'same_username_pattern', 'strength': 0.7,
                            'metadata': {'username': uname}})

    for comp, cids in company_map.items():
        if len(cids) > 1:
            for i in range(len(cids)):
                for j in range(i+1, len(cids)):
                    key = tuple(sorted([cids[i], cids[j]]))
                    if key not in seen:
                        seen.add(key)
                        relationships.append({
                            'contact_a_id': cids[i], 'contact_b_id': cids[j],
                            'relationship_type': 'same_company', 'strength': 0.6,
                            'metadata': {'company': comp}})

    for acc_id, cids in account_map.items():
        cids_list = sorted(cids)
        for i in range(len(cids_list)):
            for j in range(i+1, min(i+20, len(cids_list))):
                key = tuple(sorted([cids_list[i], cids_list[j]]))
                if key not in seen:
                    seen.add(key)
                    relationships.append({
                        'contact_a_id': cids_list[i], 'contact_b_id': cids_list[j],
                        'relationship_type': 'mutual_account', 'strength': 0.3,
                        'metadata': {'account_id': acc_id}})

    saved = 0
    for rel in relationships:
        try:
            await pool.execute(
                '''INSERT INTO contact_relationships
                   (owner_id, contact_a_id, contact_b_id, relationship_type, strength, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                   ON CONFLICT (owner_id, contact_a_id, contact_b_id, relationship_type)
                   DO UPDATE SET strength=EXCLUDED.strength, metadata=EXCLUDED.metadata, updated_at=NOW()''',
                owner_id, rel['contact_a_id'], rel['contact_b_id'],
                rel['relationship_type'], rel['strength'],
                json.dumps(rel.get('metadata', {})))
            saved += 1
        except Exception as e:
            log.warning("save_relationship error: %s", e)

    return {'relationships': relationships, 'count': saved}


async def get_relationships(pool, owner_id: int, contact_id: str) -> list:
    rows = await pool.fetch(
        '''SELECT r.*, uc.first_name, uc.last_name, uc.username, uc.is_premium
           FROM contact_relationships r
           JOIN unified_contacts uc ON (uc.id = r.contact_b_id OR uc.id = r.contact_a_id) AND uc.id != $3
           WHERE r.owner_id = $1 AND (r.contact_a_id = $3 OR r.contact_b_id = $3)
           ORDER BY r.strength DESC LIMIT 50''',
        owner_id, contact_id, contact_id)
    result = []
    for r in rows:
        d = dict(r)
        d['related_contact_name'] = f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip() or d.pop('username', '') or '?'
        d['related_is_premium'] = d.pop('is_premium', False)
        result.append(d)
    return result


async def add_relationship(pool, contact_a_id: str, contact_b_id: str,
                           relationship_type: str, strength: float = 0.5,
                           metadata: dict = None) -> dict:
    owner_row = await pool.fetchrow(
        'SELECT owner_id FROM unified_contacts WHERE id=$1', contact_a_id)
    if not owner_row:
        return {'error': 'contact_not_found'}
    owner_id = owner_row['owner_id']
    await pool.execute(
        '''INSERT INTO contact_relationships
           (owner_id, contact_a_id, contact_b_id, relationship_type, strength, metadata)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb)
           ON CONFLICT (owner_id, contact_a_id, contact_b_id, relationship_type)
           DO UPDATE SET strength=EXCLUDED.strength, metadata=EXCLUDED.metadata, updated_at=NOW()''',
        owner_id, contact_a_id, contact_b_id, relationship_type, strength,
        json.dumps(metadata or {}))
    return {'ok': True, 'relationship_type': relationship_type, 'strength': strength}


async def remove_relationship(pool, relationship_id: int) -> bool:
    result = await pool.execute(
        'DELETE FROM contact_relationships WHERE id=$1', relationship_id)
    return result != 'DELETE 0'


async def get_graph_stats(pool, owner_id: int) -> dict:
    total_rel = await pool.fetchval(
        'SELECT COUNT(*) FROM contact_relationships WHERE owner_id=$1', owner_id)
    by_type = await pool.fetch(
        '''SELECT relationship_type, COUNT(*) as cnt
           FROM contact_relationships WHERE owner_id=$1
           GROUP BY relationship_type ORDER BY cnt DESC''', owner_id)
    strongest = await pool.fetch(
        '''SELECT r.contact_a_id, r.contact_b_id, r.relationship_type, r.strength,
                  uc1.first_name as a_name, uc2.first_name as b_name
           FROM contact_relationships r
           JOIN unified_contacts uc1 ON uc1.id = r.contact_a_id
           JOIN unified_contacts uc2 ON uc2.id = r.contact_b_id
           WHERE r.owner_id = $1
           ORDER BY r.strength DESC LIMIT 10''', owner_id)
    return {
        'total_relationships': total_rel,
        'by_type': [dict(r) for r in by_type],
        'strongest': [dict(r) for r in strongest],
    }
