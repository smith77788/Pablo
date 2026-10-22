from __future__ import annotations
import json
import logging
from typing import Optional

import asyncpg

# Поля, которые версия обязана нести: ровно те, что владелец правит и что
# умеет вернуть откат. Список лежал в трёх местах — у правки, у откатчика и у
# сборки снимка, — значит поле, добавленное в одно, молча терялось в
# остальных. Канонический список один и живёт в репозитории.
from services.contacts_hub.repository import (
    EDITABLE_FIELDS as RESTORABLE_FIELDS, coerce_date)

log = logging.getLogger(__name__)

def snapshot_of(contact: dict) -> dict:
    """Снимок контакта для версии: только восстановимые поля, только JSON-безопасно.

    Полная строка не годится: created_at/updated_at/birthday приходят из базы
    как datetime/date, а снимок уходит в json.dumps — он на них падает, и
    версия не создалась бы вовсе (а правка контакта при этом прошла).
    """
    import datetime as _dt

    out: dict = {}
    for field in RESTORABLE_FIELDS:
        if field not in contact:
            continue
        value = contact[field]
        if isinstance(value, (_dt.date, _dt.datetime)):
            value = value.isoformat()
        out[field] = value
    return out


# Подписи полей для владельца. `change_summary` печатается прямо на экране
# «Версии» — и печаталась именами столбцов: «first_name, last_name, phones».
FIELD_LABELS = {
    'first_name': 'имя',
    'last_name': 'фамилия',
    'username': 'имя в Telegram',
    'display_name': 'отображаемое имя',
    'phones': 'телефоны',
    'emails': 'почта',
    'company': 'компания',
    'position': 'должность',
    'websites': 'сайты',
    'addresses': 'адреса',
    'birthday': 'день рождения',
    'notes': 'заметки',
    'tags': 'теги',
    'color_label': 'цветная метка',
    'is_favorite': 'избранное',
    'is_premium': 'премиум',
    'importance_level': 'важность',
    'user_rating': 'оценка',
    'custom_fields': 'свои поля',
}

_JSONB = ('phones', 'emails', 'websites', 'addresses', 'custom_fields')


def _norm(field: str, value):
    """Значение в виде, в котором его можно сравнивать со вторым таким же.

    Снимок приходит из базы, правка — с экрана, и одно и то же значение
    выглядит в них по-разному: jsonb отдаётся строкой, пустое поле бывает
    NULL, пустой строкой и пустым списком, дата — то `date`, то ISO-строкой.
    Без приведения «изменённым» оказывалось КАЖДОЕ поле.
    """
    if field in _JSONB and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if value is None or value == '' or value == [] or value == {}:
        return None
    if field == 'birthday':
        return str(value)[:10]
    return value


def changed_against(snapshot: dict, updates: dict) -> list:
    """Поля, которые правка РЕАЛЬНО меняет.

    Экран отправляет карточку целиком, поэтому «всё, что пришло» — это всегда
    все поля: версия подписывалась перечислением всей карточки и утверждала,
    что изменились телефоны, которых владелец не касался.
    """
    return [f for f in RESTORABLE_FIELDS
            if f in updates and _norm(f, updates[f]) != _norm(f, snapshot.get(f))]


def summary_of(fields) -> Optional[str]:
    """Подпись версии по-русски: её читает владелец, а не разработчик."""
    if not fields:
        return None
    return ', '.join(FIELD_LABELS.get(f, f) for f in fields)


async def create_version(pool, contact_id: str, owner_id: int, snapshot: dict,
                         changed_fields: list = None, changed_by: str = 'sync',
                         change_summary: str = None) -> int:
    conn = await pool.acquire()
    try:
        async with conn.transaction():
            row = await conn.fetchval(
                'SELECT COALESCE(MAX(version_num), 0) FROM contact_versions WHERE contact_id=$1',
                contact_id)
            version_num = row + 1
            version_id = await conn.fetchval(
                '''INSERT INTO contact_versions (contact_id, owner_id, version_num, snapshot,
                    changed_fields, changed_by, change_summary)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id''',
                contact_id, owner_id, version_num, json.dumps(snapshot),
                changed_fields or [], changed_by, change_summary)
    finally:
        await pool.release(conn)
    return version_id


async def get_versions(pool, contact_id: str, owner_id: int, limit: int = 50) -> list:
    rows = await pool.fetch(
        '''SELECT id, version_num, changed_fields, changed_by, change_summary, created_at
           FROM contact_versions WHERE contact_id=$1 AND owner_id=$2
           ORDER BY version_num DESC LIMIT $3''',
        contact_id, owner_id, limit)
    return [dict(r) for r in rows]


async def get_version_detail(pool, version_id: int, owner_id: int) -> Optional[dict]:
    row = await pool.fetchrow(
        'SELECT * FROM contact_versions WHERE id=$1 AND owner_id=$2', version_id, owner_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get('snapshot'), str):
        d['snapshot'] = json.loads(d['snapshot'])
    return d


async def rollback_to_version(pool, contact_id: str, owner_id: int, version_num: int) -> bool:
    ver = await pool.fetchrow(
        'SELECT snapshot FROM contact_versions WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_num)
    if not ver:
        return False
    snapshot = ver['snapshot']
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    # Снимок ТЕКУЩЕГО состояния — до перезаписи. Версия, которую откат
    # оставлял после себя, несла то состояние, К КОТОРОМУ откатились, то есть
    # дубль выбранной версии: состояние ДО отката не сохранялось нигде, и сам
    # откат отменить было нельзя — ошибочное нажатие «Откат» стирало карточку
    # без возврата.
    current = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        contact_id, owner_id)
    before = snapshot_of(dict(current)) if current else {}
    updatable = list(RESTORABLE_FIELDS)
    sets = []
    params = []
    idx = 1
    for field in updatable:
        if field in snapshot:
            val = snapshot[field]
            if field == 'birthday':
                # В снимке дата лежит ISO-строкой (json.dumps не умеет date),
                # а колонка — DATE без каста: строка уронила бы весь откат.
                val = coerce_date(val)
                sets.append(f'{field} = ${idx}')
            elif field in ('phones', 'emails', 'websites', 'addresses', 'custom_fields'):
                sets.append(f'{field} = ${idx}::jsonb')
                if not isinstance(val, str):
                    val = json.dumps(val)
            elif field == 'tags':
                sets.append(f'{field} = ${idx}::text[]')
            else:
                sets.append(f'{field} = ${idx}')
            params.append(val)
            idx += 1
    if not sets:
        return False
    sets.append('updated_at = NOW()')
    params.extend([contact_id, owner_id])
    await pool.execute(
        f'UPDATE unified_contacts SET {", ".join(sets)} WHERE id=${idx} AND owner_id=${idx+1}',
        *params)
    await create_version(pool, contact_id, owner_id, before,
                         changed_fields=sorted(before),
                         changed_by='rollback',
                         change_summary=f'Откат к v{version_num}')
    return True


async def get_contact_snapshot(pool, contact_id: str) -> dict:
    row = await pool.fetchrow('SELECT * FROM unified_contacts WHERE id=$1', contact_id)
    if not row:
        return {}
    d = dict(row)
    for k in ('phones', 'emails', 'websites', 'addresses', 'custom_fields', 'digital_footprint'):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


async def create_version_with_snapshot(pool, contact_id: str, owner_id: int,
                                       changed_by: str = 'snapshot',
                                       change_summary: str = None) -> Optional[int]:
    snapshot = await get_contact_snapshot(pool, contact_id)
    if not snapshot:
        return None
    return await create_version(pool, contact_id, owner_id, snapshot,
                                changed_by=changed_by,
                                change_summary=change_summary or 'Version snapshot')


async def compare_versions(pool, contact_id: str, owner_id: int,
                           version_a: int, version_b: int) -> Optional[dict]:
    ver_a = await pool.fetchrow(
        'SELECT version_num, snapshot, created_at FROM contact_versions '
        'WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_a)
    ver_b = await pool.fetchrow(
        'SELECT version_num, snapshot, created_at FROM contact_versions '
        'WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_b)
    if not ver_a or not ver_b:
        return None
    snap_a = json.loads(ver_a['snapshot']) if isinstance(ver_a['snapshot'], str) else ver_a['snapshot']
    snap_b = json.loads(ver_b['snapshot']) if isinstance(ver_b['snapshot'], str) else ver_b['snapshot']
    all_keys = set(snap_a.keys()) | set(snap_b.keys())
    changes = []
    for key in sorted(all_keys):
        val_a = snap_a.get(key)
        val_b = snap_b.get(key)
        if json.dumps(val_a, sort_keys=True, default=str) != json.dumps(val_b, sort_keys=True, default=str):
            changes.append({'field': key, 'from': val_a, 'to': val_b})
    return {
        'version_a': version_a,
        'version_b': version_b,
        'created_a': ver_a['created_at'].isoformat() if ver_a['created_at'] else None,
        'created_b': ver_b['created_at'].isoformat() if ver_b['created_at'] else None,
        'changes': changes,
        'total_changes': len(changes)
    }


async def get_version_diff(pool, contact_id: str, owner_id: int,
                           version_num: int) -> Optional[dict]:
    ver = await pool.fetchrow(
        'SELECT version_num, snapshot, created_at FROM contact_versions '
        'WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_num)
    if not ver:
        return None
    snap_current = json.loads(ver['snapshot']) if isinstance(ver['snapshot'], str) else ver['snapshot']
    prev_ver = await pool.fetchrow(
        'SELECT snapshot FROM contact_versions '
        'WHERE contact_id=$1 AND owner_id=$2 AND version_num=$3',
        contact_id, owner_id, version_num - 1)
    if prev_ver:
        snap_prev = json.loads(prev_ver['snapshot']) if isinstance(prev_ver['snapshot'], str) else prev_ver['snapshot']
    else:
        snap_prev = await get_contact_snapshot(pool, contact_id)
    if not snap_prev:
        return {'version_num': version_num, 'changes': [], 'total_changes': 0}
    all_keys = set(snap_current.keys()) | set(snap_prev.keys())
    changes = []
    for key in sorted(all_keys):
        val_prev = snap_prev.get(key)
        val_curr = snap_current.get(key)
        if json.dumps(val_prev, sort_keys=True, default=str) != json.dumps(val_curr, sort_keys=True, default=str):
            changes.append({'field': key, 'from': val_prev, 'to': val_curr})
    return {
        'version_num': version_num,
        'base_version': version_num - 1 if prev_ver else 'current',
        'created_at': ver['created_at'].isoformat() if ver['created_at'] else None,
        'changes': changes,
        'total_changes': len(changes)
    }
