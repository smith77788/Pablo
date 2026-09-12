from __future__ import annotations
import json
import logging
import uuid
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# jsonb-колонки asyncpg отдаёт СТРОКОЙ (кодек jsonb не зарегистрирован), а фронт
# ждёт массив/объект: c.phones.join(...) на строке падает «join is not a
# function». Разбираем на границе чтения — единый контракт для всех читателей.
_JSON_LIST_FIELDS = ('phones', 'emails', 'websites', 'addresses')
_JSON_OBJ_FIELDS = ('custom_fields', 'digital_footprint')


def _parse_json_fields(d: dict) -> dict:
    for f in _JSON_LIST_FIELDS:
        v = d.get(f)
        if isinstance(v, str):
            try:
                d[f] = json.loads(v)
            except (ValueError, TypeError):
                d[f] = []
        elif v is None:
            d[f] = []
    for f in _JSON_OBJ_FIELDS:
        v = d.get(f)
        if isinstance(v, str):
            try:
                d[f] = json.loads(v)
            except (ValueError, TypeError):
                d[f] = {}
    return d


def _segment_where(owner_id, f: dict):
    """Единый конструктор WHERE для среза контактов — общий для списка
    (get_contacts) и резолвера сегмента (resolve_segment/count_segment), чтобы
    фильтр «что вижу в списке» и «на что применяю действие» ГАРАНТИРОВАННО
    совпадали. Возвращает (where_sql, params, next_idx). Колонки неквалифицированы
    (одна таблица unified_contacts); коррелированные подзапросы — по .id.
    """
    conds = ['owner_id = $1']
    params = [owner_id]
    idx = 2
    # Личные/исключённые контакты не попадают в РАБОЧИЙ срез (рассылки/инвайты по
    # сегменту). По умолчанию исключаем; просмотр списка передаёт include_excluded,
    # а управление личными — excluded_only.
    if f.get('excluded_only'):
        conds.append('excluded = TRUE')
    elif not f.get('include_excluded'):
        conds.append('excluded = FALSE')
    if f.get('search'):
        conds.append(
            f'(first_name ILIKE ${idx} OR last_name ILIKE ${idx} OR '
            f'username ILIKE ${idx} OR display_name ILIKE ${idx} OR '
            f'CAST(telegram_user_id AS TEXT) ILIKE ${idx} OR '
            f'notes ILIKE ${idx} OR company ILIKE ${idx})')
        params.append(f"%{f['search']}%"); idx += 1
    if f.get('favorite_only'):
        conds.append('is_favorite = TRUE')
    if f.get('premium_only'):
        conds.append('is_premium = TRUE')
    if f.get('mutual_only'):
        conds.append('is_mutual = TRUE')
    if f.get('multi_only'):
        conds.append('(SELECT COUNT(*) FROM contact_sources WHERE contact_id = unified_contacts.id) > 1')
    if f.get('account_id'):
        conds.append(f'EXISTS (SELECT 1 FROM contact_sources WHERE contact_id = unified_contacts.id AND account_id = ${idx})')
        params.append(f['account_id']); idx += 1
    if f.get('tag'):
        conds.append(f'${idx} = ANY(tags)')
        params.append(f['tag']); idx += 1
    if f.get('group_id'):
        conds.append(f'EXISTS (SELECT 1 FROM contact_group_members WHERE contact_id = unified_contacts.id AND group_id = ${idx})')
        params.append(f['group_id']); idx += 1
    # Пол: 'm'/'f' — точное совпадение; 'unknown' — не определён (NULL).
    g = f.get('gender')
    if g in ('m', 'f'):
        conds.append(f'gender = ${idx}'); params.append(g); idx += 1
    elif g == 'unknown':
        conds.append('gender IS NULL')
    # CRM-стадия — из отдельной таблицы contact_crm.
    if f.get('crm_stage'):
        conds.append(f'EXISTS (SELECT 1 FROM contact_crm cc WHERE cc.contact_id = unified_contacts.id AND cc.stage = ${idx})')
        params.append(f['crm_stage']); idx += 1
    return ' AND '.join(conds), params, idx


async def get_contacts(pool, owner_id, search=None, tag=None, group_id=None,
                       favorite_only=False, premium_only=False, multi_only=False,
                       mutual_only=False, gender=None, crm_stage=None,
                       account_id=None, excluded_only=False,
                       sort_by='first_name', limit=100, offset=0) -> dict:
    # Просмотр списка показывает ВСЕ контакты (в т.ч. личные — их надо видеть и
    # уметь снять пометку); excluded_only — режим управления только личными.
    filters = {'search': search, 'tag': tag, 'group_id': group_id,
               'favorite_only': favorite_only, 'premium_only': premium_only,
               'multi_only': multi_only, 'mutual_only': mutual_only,
               'gender': gender, 'crm_stage': crm_stage, 'account_id': account_id,
               'excluded_only': excluded_only,
               'include_excluded': not excluded_only}
    where, params, idx = _segment_where(owner_id, filters)
    sort_map = {
        'name': 'first_name, last_name',
        'username': 'username',
        'accounts': '(SELECT COUNT(*) FROM contact_sources WHERE contact_id = unified_contacts.id) DESC',
        'discovered': 'discovered_at DESC',
        'synced': 'last_synced_at DESC',
        'changed': 'last_changed_at DESC',
    }
    order = sort_map.get(sort_by, 'first_name')
    params.extend([limit, offset])
    rows = await pool.fetch(f'SELECT * FROM unified_contacts WHERE {where} ORDER BY {order} LIMIT ${idx} OFFSET ${idx+1}', *params)
    total = await pool.fetchval(f'SELECT COUNT(*) FROM unified_contacts WHERE {where}', *params[:-2])
    return {'contacts': [_parse_json_fields(dict(r)) for r in rows], 'total': total}


async def count_segment(pool, owner_id, filters: dict) -> int:
    """Сколько контактов в срезе по фильтру (для превью до действия)."""
    where, params, _ = _segment_where(owner_id, filters)
    return int(await pool.fetchval(
        f'SELECT COUNT(*) FROM unified_contacts WHERE {where}', *params) or 0)


_SEG_FILTER_KEYS = ("search", "tag", "group_id", "favorite_only", "premium_only",
                    "multi_only", "mutual_only", "gender", "crm_stage", "account_id")


def _clean_filters(filters: dict) -> dict:
    """Оставить только валидные ключи фильтра (то, что понимает _segment_where)."""
    return {k: filters[k] for k in _SEG_FILTER_KEYS if filters.get(k) not in (None, "", False)}


async def save_segment(pool, owner_id, name: str, filters: dict) -> int:
    name = (name or "").strip()[:80] or "Сегмент"
    import json as _json
    return int(await pool.fetchval(
        "INSERT INTO saved_segments(owner_id, name, filters) VALUES($1,$2,$3::jsonb) RETURNING id",
        owner_id, name, _json.dumps(_clean_filters(filters or {}))))


async def list_segments(pool, owner_id) -> list[dict]:
    """Сохранённые сегменты владельца + актуальный размер каждого (live count)."""
    import json as _json
    rows = await pool.fetch(
        "SELECT id, name, filters, created_at FROM saved_segments WHERE owner_id=$1 "
        "ORDER BY created_at DESC", owner_id)
    out = []
    for r in rows:
        f = r["filters"]
        if isinstance(f, str):
            try: f = _json.loads(f)
            except Exception: f = {}
        try:
            n = await count_segment(pool, owner_id, f)
        except Exception:
            n = None
        out.append({"id": int(r["id"]), "name": r["name"], "filters": f, "count": n})
    return out


async def get_segment_filters(pool, owner_id, segment_id: int) -> dict | None:
    import json as _json
    row = await pool.fetchrow(
        "SELECT filters FROM saved_segments WHERE id=$1 AND owner_id=$2", segment_id, owner_id)
    if not row:
        return None
    f = row["filters"]
    if isinstance(f, str):
        try: f = _json.loads(f)
        except Exception: f = {}
    return f or {}


async def delete_segment(pool, owner_id, segment_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM saved_segments WHERE id=$1 AND owner_id=$2", segment_id, owner_id)
    return not str(res).endswith(" 0")


async def resolve_segment(pool, owner_id, filters: dict, limit: int = 5000) -> list:
    """Срез контактов для массового действия: только поля, нужные для адресации
    (username / telegram_user_id / phones). Тот же WHERE, что и у списка."""
    where, params, idx = _segment_where(owner_id, filters)
    params.append(limit)
    rows = await pool.fetch(
        'SELECT id, username, telegram_user_id, first_name, last_name, phones '
        f'FROM unified_contacts WHERE {where} ORDER BY id LIMIT ${idx}', *params)
    return [_parse_json_fields(dict(r)) for r in rows]


async def get_contact(pool, contact_id, owner_id):
    row = await pool.fetchrow('SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2', contact_id, owner_id)
    if not row:
        return None
    sources = await pool.fetch('SELECT * FROM contact_sources WHERE contact_id=$1', contact_id)
    history = await pool.fetch('SELECT * FROM contact_history WHERE contact_id=$1 ORDER BY created_at DESC LIMIT 50', contact_id)
    groups = await pool.fetch('SELECT g.* FROM contact_groups g JOIN contact_group_members gm ON g.id = gm.group_id WHERE gm.contact_id = $1', contact_id)
    return {
        'contact': _parse_json_fields(dict(row)),
        'sources': [dict(s) for s in sources],
        'history': [dict(h) for h in history],
        'groups': [dict(g) for g in groups],
    }


async def upsert_contact(pool, owner_id, data: dict) -> str:
    contact_id = data.get('id') or str(uuid.uuid4())
    # RETURNING id: на конфликте по (owner, telegram_user_id) возвращаем id
    # СУЩЕСТВУЮЩЕЙ строки, а не только что сгенерированный uuid. Иначе вызывающий
    # привязывал бы CRM/теги/источники к несуществующему id (тихая потеря данных).
    row = await pool.fetchrow(
        '''INSERT INTO unified_contacts (id, owner_id, telegram_user_id, username, first_name, last_name,
            display_name, phones, is_premium, discovered_at, last_synced_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)
           ON CONFLICT (owner_id, telegram_user_id) DO UPDATE SET
            username = COALESCE(EXCLUDED.username, unified_contacts.username),
            first_name = COALESCE(EXCLUDED.first_name, unified_contacts.first_name),
            last_name = COALESCE(EXCLUDED.last_name, unified_contacts.last_name),
            display_name = COALESCE(EXCLUDED.display_name, unified_contacts.display_name),
            phones = EXCLUDED.phones,
            is_premium = EXCLUDED.is_premium,
            last_synced_at = EXCLUDED.last_synced_at,
            updated_at = NOW()
           RETURNING id''',
        contact_id, owner_id, data.get('telegram_user_id'), data.get('username'),
        data.get('first_name'), data.get('last_name'), data.get('display_name'),
        json.dumps(data.get('phones', [])), data.get('is_premium', False),
        data.get('discovered_at'), data.get('last_synced_at'),
    )
    return row['id'] if row else contact_id


async def update_contact(pool, contact_id, owner_id, updates: dict) -> bool:
    set_clauses = []
    params = []
    idx = 1
    for field in ('first_name','last_name','username','display_name','phones','emails','company','position','websites','addresses','birthday','notes','tags','color_label','is_favorite','is_premium','importance_level','user_rating','custom_fields'):
        if field in updates:
            val = updates[field]
            if field in ('phones','emails','websites','addresses','custom_fields'):
                set_clauses.append(f'{field} = ${idx}::jsonb')
                params.append(json.dumps(val))
            elif field == 'tags':
                set_clauses.append(f'{field} = ${idx}::text[]')
                params.append(val)
            else:
                set_clauses.append(f'{field} = ${idx}')
                params.append(val)
            idx += 1
    set_clauses.append('updated_at = NOW()')
    params.extend([contact_id, owner_id])
    result = await pool.execute(f'UPDATE unified_contacts SET {", ".join(set_clauses)} WHERE id=${idx} AND owner_id=${idx+1}', *params)
    return result != 'UPDATE 0'


async def delete_contact(pool, contact_id, owner_id) -> bool:
    result = await pool.execute('DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2', contact_id, owner_id)
    return result != 'DELETE 0'


async def get_contact_groups(pool, owner_id) -> list:
    rows = await pool.fetch('SELECT g.*, COUNT(gm.contact_id) as member_count FROM contact_groups g LEFT JOIN contact_group_members gm ON g.id = gm.group_id WHERE g.owner_id = $1 GROUP BY g.id ORDER BY g.name', owner_id)
    return [dict(r) for r in rows]


async def add_contact_to_group(pool, contact_id, group_id) -> bool:
    try:
        await pool.execute('INSERT INTO contact_group_members (group_id, contact_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', group_id, contact_id)
        return True
    except Exception:
        return False


async def remove_contact_from_group(pool, contact_id, group_id) -> bool:
    result = await pool.execute('DELETE FROM contact_group_members WHERE group_id=$1 AND contact_id=$2', group_id, contact_id)
    return result != 'DELETE 0'


async def get_contact_stats(pool, owner_id) -> dict:
    total = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1', owner_id)
    premium = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_premium=TRUE', owner_id)
    with_username = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND username IS NOT NULL', owner_id)
    with_phone = await pool.fetchval("SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND phones != '[]'", owner_id)
    favorites = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_favorite=TRUE', owner_id)
    mutual = await pool.fetchval('SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND is_mutual=TRUE', owner_id)
    notes_count = await pool.fetchval("SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1 AND notes != '' AND notes IS NOT NULL", owner_id)
    tag_count = await pool.fetchval("SELECT COUNT(DISTINCT tag) FROM unified_contacts, unnest(tags) AS tag WHERE owner_id=$1", owner_id)
    group_count = await pool.fetchval('SELECT COUNT(*) FROM contact_groups WHERE owner_id=$1', owner_id)
    last_sync = await pool.fetchval('SELECT MAX(last_synced_at) FROM unified_contacts WHERE owner_id=$1', owner_id)
    accounts = await pool.fetch('SELECT cs.account_id, COUNT(*) as cnt FROM contact_sources cs JOIN unified_contacts uc ON cs.contact_id = uc.id WHERE uc.owner_id = $1 GROUP BY cs.account_id', owner_id)
    return {
        'total': total, 'premium': premium, 'with_username': with_username,
        'with_phone': with_phone, 'favorites': favorites, 'mutual': mutual, 'notes': notes_count,
        'tags': tag_count, 'groups': group_count, 'last_sync': last_sync,
        'by_account': [{'account_id': a['account_id'], 'count': a['cnt']} for a in accounts],
    }


async def log_contact_history(pool, contact_id, owner_id, action, field_name=None, old_value=None, new_value=None, source='user'):
    await pool.execute('INSERT INTO contact_history (contact_id, owner_id, action, field_name, old_value, new_value, source) VALUES ($1,$2,$3,$4,$5,$6,$7)',
        contact_id, owner_id, action, field_name, old_value, new_value, source)


async def log_sync(pool, owner_id, account_id, sync_type, synced, created, updated, merged, duration_ms, error=None):
    await pool.execute('INSERT INTO contact_sync_log (owner_id, account_id, sync_type, contacts_synced, contacts_created, contacts_updated, contacts_merged, duration_ms, error_message, finished_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())',
        owner_id, account_id, sync_type, synced, created, updated, merged, duration_ms, error)
