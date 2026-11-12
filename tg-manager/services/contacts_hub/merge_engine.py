from __future__ import annotations
import json
import logging

import asyncpg

from services.contacts_hub.identity_keys import merge_emails, merge_phones
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


# Поля, которые слияние обязано объединять ПО СМЫСЛУ, а не «заполнить пустое».
#
# НАХОДКА (12.11.2026). Правило «скалярное поле берётся у второго, только если
# у основного пусто» для флагов и чисел не работает: False и 0 — не «пусто».
# Отметка «⭐ избранное» второй записи, её оценка 5 из 5 и важность пропадали,
# если у основной стояли значения по умолчанию.
_FLAG_OR = ('is_favorite', 'is_premium', 'is_mutual', 'is_verified')
_MAX_INT = ('importance_level', 'user_rating')

# Поля личности, которых нет среди РЕДАКТИРУЕМЫХ (их пишет синхронизация, а не
# экран), и поэтому перенос по EDITABLE_FIELDS их не видел вовсе. Самое
# дорогое — telegram_user_id: основная запись из импорта (только телефон)
# поглощала запись из синхронизации и теряла Telegram ID. Человек переставал
# быть адресуемым в ЛС, а следующая синхронизация заводила его заново — то
# есть слияние не убирало дубль, а возвращало его, уже без истории.
_IDENTITY_FILL = ('telegram_user_id', 'patronymic', 'gender', 'registered_estimate')


def _later(a, b) -> bool:
    """b позже a (None — самое раннее)."""
    if b is None:
        return False
    if a is None:
        return True
    try:
        return b > a
    except TypeError:
        return False


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
        if field in _FLAG_OR:
            if sec and not primary.get(field):
                out[field] = True
            continue
        if field in _MAX_INT:
            try:
                if int(sec or 0) > int(primary.get(field) or 0):
                    out[field] = int(sec)
            except (TypeError, ValueError):
                pass
            continue
        if field in LIST_FIELDS:
            a, b = _as_list(primary.get(field)), _as_list(sec)
            if field == 'phones':
                # Один номер в разных написаниях (`+7999…` и `8 999 …`) — один
                # номер: строкой их сравнивать нельзя, иначе после слияния в
                # карточке три записи одного телефона (identity_keys).
                merged = merge_phones(a, b)
            elif field == 'emails':
                merged = merge_emails(a, b)
            else:
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
    _absorb_identity(primary, secondary, out)
    return out


def _absorb_identity(primary: dict, secondary: dict, out: dict) -> None:
    """Поля личности и отметки, которые пишет не экран, а синхронизация.

    * Telegram ID, отчество, пол, оценка даты регистрации — заполнить пустое;
    * «взаимный», «подтверждён» — ИЛИ (см. _FLAG_OR);
    * «был в сети» и последняя активность — более поздняя;
    * «впервые найден» — более ранняя: человек известен с первой встречи;
    * «не писать» (excluded) — ЛИПКАЯ: если хоть одна запись была исключена
      из рассылок, объединённая исключена тоже. Иначе слияние снимало отказ:
      человек, попросивший больше не писать, после «Объединить» снова попадал
      в срез рассылки.
    Ключи берутся только те, что есть в строке: снимок для предпросмотра и
    тесты на словарях не обязаны нести все колонки таблицы.
    """
    for field in _IDENTITY_FILL:
        if field in secondary and _empty(primary.get(field)) and not _empty(secondary.get(field)):
            out[field] = secondary[field]
    for field in ('is_mutual', 'is_verified'):
        if secondary.get(field) and not primary.get(field):
            out[field] = True
    if secondary.get('excluded') and not primary.get('excluded'):
        out['excluded'] = True
        if secondary.get('excluded_at') is not None:
            out['excluded_at'] = secondary['excluded_at']
    if _later(primary.get('last_seen_at'), secondary.get('last_seen_at')):
        out['last_seen_at'] = secondary['last_seen_at']
        if secondary.get('last_seen_type'):
            out['last_seen_type'] = secondary['last_seen_type']
    if _later(primary.get('last_active_at'), secondary.get('last_active_at')):
        out['last_active_at'] = secondary['last_active_at']
    pd, sd = primary.get('discovered_at'), secondary.get('discovered_at')
    if pd is not None and sd is not None and _later(sd, pd):
        out['discovered_at'] = sd


# Почему пара не слилась — по-русски, НА СЕРВЕРЕ (как STAGE_LABELS): экран
# показывает это владельцу как есть.
MERGE_ERRORS = {
    'primary_not_found': 'Основной контакт не найден — возможно, он уже объединён',
    'secondary_not_found': 'Второй контакт не найден — возможно, он уже объединён',
    'different_telegram_accounts': ('Это два разных аккаунта Telegram — '
                                    'при объединении один из них потерялся бы'),
}


def telegram_conflict(primary: dict, secondary: dict) -> bool:
    """Две записи — два РАЗНЫХ аккаунта Telegram.

    Строка контакта держит один telegram_user_id. Слить два разных значит
    молча выбросить один аккаунт: в ЛС по нему больше не написать, а
    синхронизация заведёт его снова. Такую пару слияние отклоняет, а поиск
    дублей не предлагает (trust_engine.compute_merge_confidence).
    """
    a, b = primary.get('telegram_user_id'), secondary.get('telegram_user_id')
    return bool(a) and bool(b) and int(a) != int(b)


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

    if telegram_conflict(dict(primary), dict(secondary)):
        return {'error': 'different_telegram_accounts'}

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
        moved = await rehome_dependents(conn, primary_id, secondary_id, owner_id)
        await conn.execute(
            'DELETE FROM unified_contacts WHERE id=$1 AND owner_id=$2',
            secondary_id, owner_id)
    return {'before': before, 'taken': taken, 'moved': moved}


async def rehome_dependents(conn, primary_id, secondary_id, owner_id: int) -> dict:
    """Перенести на основной контакт ВСЁ, что висит на втором.

    НАХОДКА (12.11.2026). Слияние перецепляло только источники и историю
    правок, а у второго контакта ещё девять таблиц на `ON DELETE CASCADE`.
    Удаление второй записи молча стирало:
      * его СДЕЛКУ (contact_crm: стадия, сумма, напоминание) — клиент в
        «Переговорах» на 300 000 ₽ после «Объединить» пропадал с доски;
      * журнал касаний (contact_crm_activity) — звонки и встречи;
      * членство в группах — человек выпадал из «VIP» и из рассылок по ней;
      * авто-теги, граф личности, связи с другими контактами, расхождения.
    Подтверждение при этом обещало: «её данные перенесутся в первую».

    И отдельно: `UPDATE contact_sources SET contact_id=…` падал целиком, если
    оба контакта пришли из ОДНОГО аккаунта (UNIQUE(contact_id, account_id)) —
    а это ровно типичный дубль: тот же аккаунт видит человека и по номеру, и
    по нику. Владелец получал «Внутренняя ошибка», и пара не сливалась никогда.

    Правила те же, что у полей: у основного ничего не перезаписывается, у
    второго ничего не теряется. Возвращает, сколько чего переехало.
    """
    moved: dict = {}

    # Источники: общий аккаунт — одна строка (с самой ранней встречей).
    await conn.execute(
        '''UPDATE contact_sources p
              SET first_seen_at = LEAST(p.first_seen_at, s.first_seen_at),
                  last_synced_at = GREATEST(p.last_synced_at, s.last_synced_at)
             FROM contact_sources s
            WHERE p.contact_id = $1 AND s.contact_id = $2
              AND p.account_id = s.account_id''',
        primary_id, secondary_id)
    await conn.execute(
        '''DELETE FROM contact_sources s
            WHERE s.contact_id = $2
              AND EXISTS (SELECT 1 FROM contact_sources p
                           WHERE p.contact_id = $1 AND p.account_id = s.account_id)''',
        primary_id, secondary_id)
    await conn.execute(
        'UPDATE contact_sources SET contact_id=$1 WHERE contact_id=$2',
        primary_id, secondary_id)
    await conn.execute(
        'UPDATE contact_history SET contact_id=$1 WHERE contact_id=$2',
        primary_id, secondary_id)

    moved['deal'] = await _absorb_crm(conn, primary_id, secondary_id, owner_id)
    moved['touches'] = _count(await conn.execute(
        'UPDATE contact_crm_activity SET contact_id=$1 '
        'WHERE contact_id=$2 AND owner_id=$3',
        primary_id, secondary_id, owner_id))
    # Группы: член хотя бы одной записи — член объединённой. Группа обязана
    # быть своей: join по владельцу — замок, а не оптимизация.
    moved['groups'] = _count(await conn.execute(
        '''INSERT INTO contact_group_members (group_id, contact_id)
           SELECT m.group_id, $1 FROM contact_group_members m
             JOIN contact_groups g ON g.id = m.group_id AND g.owner_id = $3
            WHERE m.contact_id = $2
           ON CONFLICT DO NOTHING''',
        primary_id, secondary_id, owner_id))
    moved['smart_tags'] = _count(await conn.execute(
        '''INSERT INTO contact_smart_tags
               (owner_id, contact_id, tag, source, confidence, rule_id, applied_at)
           SELECT owner_id, $1, tag, source, confidence, rule_id, applied_at
             FROM contact_smart_tags WHERE contact_id = $2 AND owner_id = $3
           ON CONFLICT (owner_id, contact_id, tag) DO NOTHING''',
        primary_id, secondary_id, owner_id))
    # Граф личности: «основная» отметка переезжает, только если у основного
    # контакта своей основной того же типа нет — двух основных быть не должно.
    await conn.execute(
        '''INSERT INTO contact_identity_graph
               (owner_id, contact_id, identity_type, identity_value, is_primary,
                confidence, source, verified, created_at)
           SELECT s.owner_id, $1, s.identity_type, s.identity_value,
                  s.is_primary AND NOT EXISTS (
                      SELECT 1 FROM contact_identity_graph p
                       WHERE p.contact_id = $1 AND p.owner_id = $3
                         AND p.identity_type = s.identity_type AND p.is_primary),
                  s.confidence, s.source, s.verified, s.created_at
             FROM contact_identity_graph s
            WHERE s.contact_id = $2 AND s.owner_id = $3
           ON CONFLICT (owner_id, contact_id, identity_type, identity_value) DO NOTHING''',
        primary_id, secondary_id, owner_id)
    await conn.execute(
        'UPDATE contact_conflicts SET contact_id=$1 WHERE contact_id=$2 AND owner_id=$3',
        primary_id, secondary_id, owner_id)
    # Связи: связь двух записей между собой после слияния — петля, её нет;
    # остальные перецепляются, если у основного нет такой же.
    await conn.execute(
        '''DELETE FROM contact_relationships
            WHERE owner_id = $3
              AND ((contact_a_id = $1 AND contact_b_id = $2)
                OR (contact_a_id = $2 AND contact_b_id = $1))''',
        primary_id, secondary_id, owner_id)
    for col, other in (('contact_a_id', 'contact_b_id'), ('contact_b_id', 'contact_a_id')):
        await conn.execute(
            f'''UPDATE contact_relationships r SET {col} = $1
                 WHERE r.owner_id = $3 AND r.{col} = $2
                   AND NOT EXISTS (
                       SELECT 1 FROM contact_relationships x
                        WHERE x.owner_id = r.owner_id AND x.{col} = $1
                          AND x.{other} = r.{other}
                          AND x.relationship_type = r.relationship_type)''',
            primary_id, secondary_id, owner_id)
    # Связка с внешней CRM (amoCRM/Bitrix24): без переноса следующее событие
    # по объединённому человеку завело бы в CRM ВТОРУЮ карточку.
    await conn.execute(
        '''UPDATE crm_links l SET local_key = 'contact:' || $1, updated_at = now()
            WHERE l.owner_id = $3 AND l.local_key = 'contact:' || $2
              AND NOT EXISTS (SELECT 1 FROM crm_links x
                               WHERE x.endpoint_id = l.endpoint_id
                                 AND x.local_key = 'contact:' || $1)''',
        str(primary_id), str(secondary_id), owner_id)
    return moved


def _count(status) -> int:
    """Число строк из статуса asyncpg ('UPDATE 3', 'INSERT 0 2')."""
    try:
        return int(str(status).rsplit(' ', 1)[-1])
    except (ValueError, IndexError):
        return 0


_CRM_FILL = ('next_reminder_at', 'next_reminder_text', 'last_message_preview',
             'assigned_to')


async def _absorb_crm(conn, primary_id, secondary_id, owner_id: int) -> bool:
    """Сделка второго контакта: переехать целиком или дополнить сделку первого.

    Нет сделки у основного — строка второго просто перецепляется (со всей
    стадией, суммой и напоминанием). Есть у обоих — у основного не
    перезаписывается ничего заполненного: стадия «Лид» (значение по
    умолчанию) уступает продвинутой стадии второго, нулевая сумма — его сумме
    ВМЕСТЕ с валютой (сумма без своей валюты — другое число), пустое
    напоминание — его напоминанию, последнее касание — более позднему.
    Возвращает True, если со сделкой второго что-то сделано.
    """
    sec = await conn.fetchrow(
        'SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2',
        owner_id, secondary_id)
    if not sec:
        return False
    sec = dict(sec)
    pri = await conn.fetchrow(
        'SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2',
        owner_id, primary_id)
    if not pri:
        if sec.get('id') is None:
            return False
        await conn.execute(
            'UPDATE contact_crm SET contact_id=$1, updated_at=NOW() WHERE id=$2',
            primary_id, sec.get('id'))
        return True
    pri = dict(pri)
    if pri.get('id') is None:
        return False
    sets, params = [], []

    def _set(col, val):
        params.append(val)
        sets.append(f'{col} = ${len(params)}')

    if (pri.get('stage') or 'lead') == 'lead' and (sec.get('stage') or 'lead') != 'lead':
        _set('stage', sec.get('stage'))
    if not (pri.get('deal_value') or 0) and (sec.get('deal_value') or 0):
        _set('deal_value', sec.get('deal_value'))
        _set('currency', sec.get('currency'))
    for col in _CRM_FILL:
        if _empty(pri.get(col)) and not _empty(sec.get(col)):
            _set(col, sec.get(col))
    if _later(pri.get('last_interaction_at'), sec.get('last_interaction_at')):
        _set('last_interaction_at', sec.get('last_interaction_at'))
        _set('last_interaction_type', sec.get('last_interaction_type'))
    a_cf, b_cf = _as_dict(pri.get('custom_fields')), _as_dict(sec.get('custom_fields'))
    add = {k: v for k, v in b_cf.items() if k not in a_cf}
    if add:
        params.append(json.dumps({**a_cf, **add}))
        sets.append(f'custom_fields = ${len(params)}::jsonb')
    if not sets:
        return False
    params.append(pri.get('id'))
    await conn.execute(
        f'UPDATE contact_crm SET {", ".join(sets)}, updated_at = NOW() '
        f'WHERE id = ${len(params)}', *params)
    return True


def _as_dict(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


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
        return {'status': 'error', 'error': absorbed['error'],
                'message': MERGE_ERRORS.get(absorbed['error'], 'Не удалось объединить')}
    await _version_after_merge(pool, primary_id, owner_id, absorbed, secondary_id)
    return {'status': 'merged', 'primary_id': primary_id, 'removed_id': secondary_id,
            'absorbed_fields': sorted(absorbed['taken']),
            'moved': absorbed.get('moved') or {}}


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
    if telegram_conflict(a, b):
        return {'error': 'different_telegram_accounts'}
    taken = absorbed_fields(a, b)
    out = []
    for field, value in taken.items():
        # Служебные спутники (время отметки «не писать», вид «был в сети»)
        # едут вместе со своим полем и отдельной строкой не показываются.
        if field not in FIELD_LABELS:
            continue
        out.append({
            'field': field,
            'label': FIELD_LABELS[field],
            'from': _plain(a.get(field)),
            'to': _plain(value),
        })
    name = ' '.join(x for x in (b.get('first_name'), b.get('last_name')) if x)
    return {
        'taken': out,
        'total': len(out),
        'secondary': name or b.get('username') or b.get('display_name') or '',
        # Кроме полей переезжает то, что висит на второй записи: сделка,
        # касания, группы. Подтверждение обязано о них сказать — раньше они
        # молча удалялись, и обещание «данные перенесутся» было неправдой.
        'attached': await _attached_counts(pool, secondary_id, owner_id),
    }


def _plain(v):
    """Значение для ответа экрану: дата/время — ISO-строкой."""
    import datetime as _dt
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return v


async def _attached_counts(pool, contact_id, owner_id: int) -> dict:
    row = await pool.fetchrow(
        '''SELECT
             (SELECT COUNT(*) FROM contact_crm
               WHERE contact_id = $1 AND owner_id = $2) AS deal,
             (SELECT COUNT(*) FROM contact_crm_activity
               WHERE contact_id = $1 AND owner_id = $2) AS touches,
             (SELECT COUNT(*) FROM contact_group_members m
                JOIN contact_groups g ON g.id = m.group_id AND g.owner_id = $2
               WHERE m.contact_id = $1) AS groups''',
        contact_id, owner_id)
    return {k: int(row[k] or 0) for k in ('deal', 'touches', 'groups')} if row else {}


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

