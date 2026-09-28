from __future__ import annotations
import json
import logging
import re
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

BUILTIN_RULES = [
    # `code` — стабильный ключ правила (в него смотрит интерфейс и по нему
    # строится id вида `builtin_premium`). `name` — то, что видит владелец,
    # поэтому он по-русски; переименование имени ключ не ломает.
    {'code': 'premium', 'name': 'Premium-подписка', 'tag': 'premium',
     'conditions': {'field': 'is_premium', 'op': 'eq', 'value': True}},
    {'code': 'has_phone', 'name': 'Есть телефон', 'tag': 'has_phone',
     'conditions': {'field': 'phones', 'op': 'not_empty'}},
    {'code': 'has_email', 'name': 'Есть почта', 'tag': 'has_email',
     'conditions': {'field': 'emails', 'op': 'not_empty'}},
    {'code': 'multi_account', 'name': 'Есть у нескольких аккаунтов', 'tag': 'multi_account',
     'conditions': {'field': 'source_accounts_count', 'op': 'gte', 'value': 2}},
    {'code': 'has_notes', 'name': 'Есть заметка', 'tag': 'has_notes',
     'conditions': {'field': 'notes', 'op': 'not_empty'}},
    {'code': 'favorite', 'name': 'В избранном', 'tag': 'favorite',
     'conditions': {'field': 'is_favorite', 'op': 'eq', 'value': True}},
    {'code': 'has_company', 'name': 'Указана компания', 'tag': 'has_company',
     'conditions': {'field': 'company', 'op': 'not_empty'}},
]

# jsonb-поля Postgres отдаёт СТРОКОЙ. Без разбора `not_empty` видел строку
# «[]» и считал её непустой: метка «есть телефон» садилась на каждый контакт
# подряд. Разбираем тем же общим парсером, что и остальные читатели контактов
# (см. tests/test_contacts_json_fields.py).
def _normalize_contact(contact: dict) -> dict:
    from services.contacts_hub.repository import _parse_json_fields
    return _parse_json_fields(dict(contact))


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


_INSERT_TAGS_SQL = """
    INSERT INTO contact_smart_tags
        (owner_id, contact_id, tag, source, confidence, rule_id)
    SELECT $1, t.cid, t.tag, t.src, 1.0, t.rid
    FROM unnest($2::uuid[], $3::text[], $4::text[], $5::int[]) AS t(cid, tag, src, rid)
    ON CONFLICT (owner_id, contact_id, tag) DO NOTHING
    RETURNING 1
"""


async def apply_smart_tags(pool, owner_id: int, contact_id: str = None) -> dict:
    """Разложить метки по контактам согласно правилам.

    Возвращает `applied` — сколько меток РЕАЛЬНО добавилось (повторный запуск
    честно покажет 0), `matched` — сколько совпадений всего, `rules_checked` —
    сколько правил проверено.
    """
    rules = await pool.fetch(
        'SELECT * FROM smart_tag_rules WHERE owner_id=$1 AND is_active=TRUE', owner_id)

    all_rules = []
    for r in rules:
        d = dict(r)
        # conditions лежат в JSONB, а asyncpg отдаёт их строкой. Без разбора
        # первое же своё правило роняло применение целиком: у строки нет
        # метода .get.
        if isinstance(d.get('conditions'), str):
            try:
                d['conditions'] = json.loads(d['conditions'])
            except (json.JSONDecodeError, TypeError):
                d['conditions'] = {}
        if not isinstance(d.get('conditions'), dict):
            d['conditions'] = {}
        d['rule_id'] = d.get('id')
        d['source'] = 'rule'
        all_rules.append(d)
    for r in BUILTIN_RULES:
        all_rules.append({
            'id': 'builtin_' + r['code'],
            'name': r['name'],
            'tag': r['tag'],
            'conditions': r['conditions'],
            # У встроенного правила нет строки в smart_tag_rules, а колонка
            # rule_id — INTEGER. Раньше туда писали «builtin_Premium», asyncpg
            # отвергал вставку, ошибку глотали — и ни одна метка не сохранялась.
            'rule_id': None,
            'source': 'builtin',
        })

    # source_accounts_count в таблице нет — это счётчик аккаунтов-источников.
    # Считаем его тем же выражением, что и фильтр «мульти-акк» в списке
    # контактов, иначе встроенное правило не совпадало НИ С ЧЕМ.
    cnt_sql = ('(SELECT COUNT(*) FROM contact_sources s WHERE s.contact_id = c.id)'
               ' AS source_accounts_count')
    if contact_id:
        contacts = await pool.fetch(
            f'SELECT c.*, {cnt_sql} FROM unified_contacts c WHERE c.owner_id=$1 AND c.id=$2',
            owner_id, contact_id)
    else:
        contacts = await pool.fetch(
            f'SELECT c.*, {cnt_sql} FROM unified_contacts c WHERE c.owner_id=$1', owner_id)

    pending = []
    for c in contacts:
        cd = _normalize_contact(dict(c))
        cid = cd['id']
        for rule in all_rules:
            tag = (rule.get('tag') or '').strip()
            if not tag:
                continue
            if _check_condition(cd, rule.get('conditions') or {}):
                pending.append((cid, tag, rule['source'], rule['rule_id']))

    # Раньше на каждое совпадение уходил отдельный INSERT: при 20 тысячах
    # контактов это 140 тысяч обращений к базе, и экран отваливался по таймауту
    # раньше, чем заканчивалась работа. Пишем пачками.
    applied = 0
    chunk = 500
    for i in range(0, len(pending), chunk):
        part = pending[i:i + chunk]
        try:
            rows = await pool.fetch(
                _INSERT_TAGS_SQL,
                owner_id,
                [r[0] for r in part], [r[1] for r in part],
                [r[2] for r in part], [r[3] for r in part])
            applied += len(rows)
        except Exception as e:
            log.warning("apply_smart_tags: пачка не записалась: %s", e)

    return {'applied': applied, 'matched': len(pending), 'rules_checked': len(all_rules)}


async def get_smart_tags(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT tag, COUNT(*) as cnt, source
           FROM contact_smart_tags WHERE owner_id=$1
           GROUP BY tag, source ORDER BY cnt DESC''', owner_id)
    return [dict(r) for r in rows]


async def get_smart_tag_rules(pool, owner_id: int) -> list:
    custom = await pool.fetch(
        'SELECT * FROM smart_tag_rules WHERE owner_id=$1 ORDER BY name', owner_id)
    result = [{'id': f"builtin_{r['code']}", 'name': r['name'], 'tag': r['tag'],
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
