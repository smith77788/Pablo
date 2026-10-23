from __future__ import annotations
import json
import logging

import asyncpg

from services.contacts_hub.repository import EDITABLE_FIELDS

log = logging.getLogger(__name__)

# Списочные поля: при слиянии ОБЪЕДИНЯЮТСЯ, а не выбирается одно из двух.
# Два телефона одного человека — это два телефона, а не «один правильный».
LIST_FIELDS = ('phones', 'emails', 'websites', 'addresses', 'tags')
_JSONB_LISTS = ('phones', 'emails', 'websites', 'addresses')


def _as_list(value):
    """Списочное поле из базы: jsonb приходит строкой, NULL — пустым списком."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value] if value.strip() else []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _empty(value) -> bool:
    return value is None or value == '' or value == [] or value == {}


def absorbed_fields(primary: dict, secondary: dict) -> dict:
    """Что перенести из поглощаемого контакта в основной.

    ПОЧЕМУ ЭТО ВООБЩЕ ЕСТЬ. Слияние дублей во всех трёх своих видах
    (manual_merge, merge_contacts_improved, bulk_merge) не переносило НИ
    ОДНОГО поля: оно перецепляло источники и историю на основной контакт и
    удаляло второй. Телефон, почта, компания, должность, заметки и теги,
    которые были только у второго, исчезали навсегда — без версии и без
    следа. Операция называлась «объединить», а делала «удалить дубль».

    Правило переноса намеренно осторожное:
      * списки объединяются (телефоны, почты, сайты, адреса, теги);
      * скалярное поле берётся у второго, ТОЛЬКО если у основного пусто;
      * непустое значение основного не перезаписывается никогда.
    Поэтому слияние не может ухудшить основной контакт — только дополнить.
    """
    out: dict = {}
    for field in EDITABLE_FIELDS:
        sec = secondary.get(field)
        if field in LIST_FIELDS:
            a, b = _as_list(primary.get(field)), _as_list(sec)
            merged = list(a)
            for item in b:
                if item not in merged:
                    merged.append(item)
            if merged != a:
                out[field] = merged
            continue
        if field == 'custom_fields':
            a = primary.get(field) or {}
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except ValueError:
                    a = {}
            b = sec or {}
            if isinstance(b, str):
                try:
                    b = json.loads(b)
                except ValueError:
                    b = {}
            if isinstance(a, dict) and isinstance(b, dict):
                add = {k: v for k, v in b.items() if k not in a}
                if add:
                    out[field] = {**a, **add}
            continue
        if _empty(primary.get(field)) and not _empty(sec):
            out[field] = sec
    return out


async def absorb_contact(conn, primary_id: str, secondary_id: str,
                         owner_id: int) -> dict | None:
    """Слить второй контакт в основной: перенести поля, перецепить источники
    и историю, удалить второй. Одна реализация на все виды слияния.

    Раньше их было три (manual_merge, merge_contacts_improved, bulk_merge), и
    все три делали ОДНО И ТО ЖЕ — перецепляли и удаляли, не перенося полей.
    Три копии одной ошибки чинились бы по одной.

    Возвращает {'before': снимок основного ДО, 'taken': перенесённые поля}
    либо {'error': 'primary_not_found'|'secondary_not_found'}. Проверка
    владения ОБОИМИ контактами обязана стоять ДО того, как мы тронем
    contact_sources/contact_history, иначе пара с чужим id — межарендный
    IDOR. Она живёт здесь и только здесь: вызывающие делали её ещё раз
    сами, то есть каждое слияние стоило двух лишних запросов, а разойтись
    эти две проверки могли в любой момент.
    """
    from services.contacts_hub.repository import coerce_date
    from services.contacts_hub.versioning_engine import snapshot_of

    primary = await conn.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        primary_id, owner_id)
    if not primary:
        return {'error': 'primary_not_found'}
    secondary = await conn.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        secondary_id, owner_id)
    if not secondary:
        return {'error': 'secondary_not_found'}

    before = snapshot_of(dict(primary))
    taken = absorbed_fields(dict(primary), dict(secondary))

    sets, params, idx = [], [], 1
    for field, value in taken.items():
        if field in _JSONB_LISTS or field == 'custom_fields':
            sets.append(f'{field} = ${idx}::jsonb')
            value = json.dumps(value)
        elif field == 'tags':
            sets.append(f'{field} = ${idx}::text[]')
        elif field == 'birthday':
            sets.append(f'{field} = ${idx}')
            value = coerce_date(value)
        else:
            sets.append(f'{field} = ${idx}')
        params.append(value)
        idx += 1

    async with conn.transaction():
        if sets:
            sets.append('updated_at = NOW()')
            params.extend([primary_id, owner_id])
            await conn.execute(
                f'UPDATE unified_contacts SET {", ".join(sets)} '
                f'WHERE id=${idx} AND owner_id=${idx + 1}', *params)
        await conn.execute(
            'UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2',
            primary_id, secondary_id)
        await conn.execute(
            'UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2',
            primary_id, secondary_id)
        await conn.execute(
            'DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2',
            secondary_id, owner_id)
    return {'before': before, 'taken': taken}


async def _version_after_merge(pool, primary_id: str, owner_id: int,
                               absorbed: dict, secondary_id: str) -> None:
    """Состояние основного ДО слияния — отдельной версией.

    Слияние необратимо: второй контакт удаляется. Версия основного — то
    единственное место, откуда владелец может вернуть карточку, если слил не
    то. Не пишется — слияние уже состоялось, ронять ответ из-за версии
    значит показать ошибку там, где всё получилось.
    """
    if not absorbed or not absorbed.get('taken'):
        return
    try:
        from services.contacts_hub.versioning_engine import (
            create_version, summary_of)
        fields = sorted(absorbed['taken'])
        await create_version(
            pool, primary_id, owner_id, absorbed['before'],
            changed_fields=fields, changed_by='merge',
            change_summary='Слияние дублей: ' + (summary_of(fields) or ''))
    except Exception:
        log.exception('версия перед слиянием не создана primary=%s', primary_id)


async def find_duplicates(pool, owner_id) -> list:
    rows = await pool.fetch(
        'SELECT telegram_user_id, array_agg(id) as ids, COUNT(*) as cnt FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id IS NOT NULL GROUP BY telegram_user_id HAVING COUNT(*) > 1',
        owner_id)
    return [{'telegram_user_id': r['telegram_user_id'], 'ids': r['ids'], 'count': r['cnt']} for r in rows]


async def auto_merge(pool, owner_id) -> dict:
    duplicates = await find_duplicates(pool, owner_id)
    merged = 0
    absorbed_log: list = []
    conn = await pool.acquire()
    try:
        for dup in duplicates:
            ids = dup['ids']
            primary = min(ids)
            others = [i for i in ids if i != primary]
            for other_id in others:
                # Тот же перенос полей, что и у ручного слияния: автоматика не
                # имеет права терять телефон, которого нет у основного.
                absorbed = await absorb_contact(conn, primary, other_id, owner_id)
                if 'error' in absorbed:
                    continue
                absorbed_log.append((primary, other_id, absorbed))
                merged += 1
    finally:
        await pool.release(conn)
    for primary_id, other_id, absorbed in absorbed_log:
        await _version_after_merge(pool, primary_id, owner_id, absorbed, other_id)
    return {'duplicates_found': len(duplicates), 'contacts_merged': merged}


async def manual_merge(pool, primary_id: str, secondary_id: str, owner_id: int) -> dict:
    # Владение обоими контактами проверяет absorb_contact — ДО того, как
    # тронет contact_sources/contact_history (межарендный IDOR: раньше
    # скоупнут owner_id был только финальный DELETE).
    conn = await pool.acquire()
    try:
        absorbed = await absorb_contact(conn, primary_id, secondary_id, owner_id)
    finally:
        await pool.release(conn)
    if 'error' in absorbed:
        return {'status': 'error', 'error': absorbed['error']}
    await _version_after_merge(pool, primary_id, owner_id, absorbed, secondary_id)
    return {'status': 'merged', 'primary_id': primary_id, 'removed_id': secondary_id,
            'absorbed_fields': sorted(absorbed['taken'])}


# УДАЛЕНО: merge_contacts_improved — слово в слово manual_merge (обе зовут
# absorb_contact и _version_after_merge). Имя с суффиксом «improved» рядом с
# рабочей функцией — это заявка на то, что следующий агент подключит к экрану
# её, а правку внесут в другую.

async def merge_preview(pool, primary_id: str, secondary_id: str,
                        owner_id: int) -> dict:
    """Что именно переедет при слиянии — ДО подтверждения.

    Здесь уже лежал `get_merge_preview`: написанный, непозванный и со СВОИМИ
    правилами слияния — восемь полей и «a или b». Правила самого слияния
    были другими, так что показать такой предпросмотр значило бы соврать
    точнее прежнего: человек увидел бы одно, а получил другое. Поэтому
    предпросмотр считает ровно тем же `absorbed_fields`, что и слияние: он
    не может разойтись с ним, потому что это он и есть.

    Возвращает {'error': ...} либо {'taken': [...], 'secondary': имя}.
    """
    primary = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        primary_id, owner_id)
    if not primary:
        return {'error': 'primary_not_found'}
    secondary = await pool.fetchrow(
        'SELECT * FROM unified_contacts WHERE id=$1 AND owner_id=$2',
        secondary_id, owner_id)
    if not secondary:
        return {'error': 'secondary_not_found'}

    from services.contacts_hub.versioning_engine import FIELD_LABELS

    a, b = dict(primary), dict(secondary)
    taken = absorbed_fields(a, b)
    out = []
    for field, value in taken.items():
        out.append({
            'field': field,
            'label': FIELD_LABELS.get(field, field),
            'from': a.get(field),
            'to': value,
        })
    name = ' '.join(x for x in (b.get('first_name'), b.get('last_name')) if x)
    return {
        'taken': out,
        'total': len(out),
        'secondary': name or b.get('username') or b.get('display_name') or '',
    }


# УДАЛЕНО: auto_merge_with_confidence(pool, owner_id, threshold=0.7).
# Автослияние «по уверенности», которое считало уверенность своим кодом и
# ошибалось так же, как ошибалась оценка дублей: `set(a['phones'])` на строке
# jsonb давало набор СИМВОЛОВ, пересечение находилось у любых двух контактов с
# телефонами, и уверенность становилась 0.8 — выше порога по умолчанию 0.7.
# А дальше шёл не перенос полей, а `DELETE FROM unified_contacts`: телефон,
# почта и заметки второй записи исчезали. Живой путь — auto_merge, он группирует
# по ТОЧНОМУ telegram_user_id и зовёт absorb_contact, то есть переносит поля.
# Функцию не звал никто, и это единственная причина, по которой она ничего не
# уничтожила. Понадобится автослияние по уверенности — строить на
# compute_merge_confidence и absorb_contact, а не на второй копии правил.

