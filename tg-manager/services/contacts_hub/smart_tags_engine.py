from __future__ import annotations
import json
import logging
import re
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

BUILTIN_RULES = [
    {'name': 'Premium', 'tag': 'premium', 'conditions': {'field': 'is_premium', 'op': 'eq', 'value': True}},
    {'name': 'With Phone', 'tag': 'has_phone', 'conditions': {'field': 'phones', 'op': 'not_empty'}},
    {'name': 'With Email', 'tag': 'has_email', 'conditions': {'field': 'emails', 'op': 'not_empty'}},
    {'name': 'Multi-Account', 'tag': 'multi_account', 'conditions': {'field': 'source_accounts_count', 'op': 'gte', 'value': 2}},
    {'name': 'Has Notes', 'tag': 'has_notes', 'conditions': {'field': 'notes', 'op': 'not_empty'}},
    {'name': 'Favorite', 'tag': 'favorite', 'conditions': {'field': 'is_favorite', 'op': 'eq', 'value': True}},
    {'name': 'With Company', 'tag': 'has_company', 'conditions': {'field': 'company', 'op': 'not_empty'}},
]


def _check_condition(contact: dict, condition: dict) -> bool:
    field = condition.get('field', '')
    op = condition.get('op', '')
    value = condition.get('value')

    val = contact.get(field)
    if op == 'eq':
        return val == value
    elif op == 'neq':
        return val != value
    elif op == 'not_empty':
        if isinstance(val, list):
            return len(val) > 0
        if isinstance(val, str):
            return bool(val.strip())
        return val is not None
    elif op == 'gte':
        try:
            return float(val or 0) >= float(value)
        except (TypeError, ValueError):
            return False
    elif op == 'lte':
        try:
            return float(val or 0) <= float(value)
        except (TypeError, ValueError):
            return False
    elif op == 'contains':
        if isinstance(val, str):
            return value.lower() in val.lower() if value else False
        return False
    elif op == 'regex':
        if isinstance(val, str) and value:
            try:
                return bool(re.search(value, val, re.IGNORECASE))
            except re.error:
                return False
        return False
    return False


async def apply_smart_tags(pool, owner_id: int, contact_id: str = None) -> dict:
    rules = await pool.fetch(
        'SELECT * FROM smart_tag_rules WHERE owner_id=$1 AND is_active=TRUE', owner_id)

    builtin_rules = []
    for r in BUILTIN_RULES:
        builtin_rules.append({
            'id': f"builtin_{r['name']}",
            'name': r['name'],
            'tag': r['tag'],
            'conditions': r['conditions'],
        })
    all_rules = [dict(r) for r in rules] + builtin_rules

    if contact_id:
        contacts = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 AND id=$2', owner_id, contact_id)
    else:
        contacts = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1', owner_id)

    applied = 0
    for c in contacts:
        cid = c['id']
        for rule in all_rules:
            cond = rule.get('conditions', {})
            if _check_condition(dict(c), cond):
                tag = rule.get('tag', '')
                if not tag:
                    continue
                try:
                    await pool.execute(
                        '''INSERT INTO contact_smart_tags (owner_id, contact_id, tag, source, confidence, rule_id)
                           VALUES ($1,$2,$3,$4,$5,$6)
                           ON CONFLICT (owner_id, contact_id, tag) DO NOTHING''',
                        owner_id, cid, tag, 'rule', 1.0,
                        str(rule.get('id', '')))
                    applied += 1
                except Exception as e:
                    log.warning("apply_smart_tag error: %s", e)

    return {'applied': applied, 'rules_checked': len(all_rules)}


async def get_smart_tags(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT tag, COUNT(*) as cnt, source
           FROM contact_smart_tags WHERE owner_id=$1
           GROUP BY tag, source ORDER BY cnt DESC''', owner_id)
    return [dict(r) for r in rows]


async def get_smart_tag_rules(pool, owner_id: int) -> list:
    custom = await pool.fetch(
        'SELECT * FROM smart_tag_rules WHERE owner_id=$1 ORDER BY name', owner_id)
    result = [{'id': f"builtin_{r['name']}", 'name': r['name'], 'tag': r['tag'],
               'conditions': r['conditions'], 'is_active': True, 'source': 'builtin'}
              for r in BUILTIN_RULES]
    for r in custom:
        d = dict(r)
        if isinstance(d.get('conditions'), str):
            try:
                d['conditions'] = json.loads(d['conditions'])
            except (json.JSONDecodeError, TypeError):
                pass
        d['source'] = 'custom'
        result.append(d)
    return result


async def create_smart_tag_rule(pool, owner_id: int, name: str, tag: str,
                                conditions: dict, rule_type: str = 'field_match') -> int:
    rule_id = await pool.fetchval(
        '''INSERT INTO smart_tag_rules (owner_id, name, rule_type, conditions, tag)
           VALUES ($1,$2,$3,$4,$5) RETURNING id''',
        owner_id, name, rule_type, json.dumps(conditions), tag)
    return rule_id


async def delete_smart_tag_rule(pool, rule_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        'DELETE FROM smart_tag_rules WHERE id=$1 AND owner_id=$2', rule_id, owner_id)
    return result != 'DELETE 0'


async def toggle_smart_tag_rule(pool, rule_id: int, owner_id: int) -> Optional[bool]:
    row = await pool.fetchrow(
        'UPDATE smart_tag_rules SET is_active = NOT is_active WHERE id=$1 AND owner_id=$2 RETURNING is_active',
        rule_id, owner_id)
    return row['is_active'] if row else None
