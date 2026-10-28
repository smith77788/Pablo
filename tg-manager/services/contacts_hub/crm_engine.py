from __future__ import annotations
import datetime as _dt
import json
import logging
import uuid as _uuid
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# Колонки contact_crm типа TIMESTAMPTZ. asyncpg биндит их по типу колонки и на
# строку кидает DataError → весь upsert падал 500 (класс #15: фича «есть», но
# при передаче даты строкой с фронта не работала никогда). Нормализуем на
# границе: ISO-строка → tz-aware datetime; naive-время трактуем как UTC.
_TS_FIELDS = ("next_reminder_at", "last_interaction_at")

# Типы касания и их подписи. Подписи живут НА СЕРВЕРЕ: экран показывал
# `activity_type` как есть, то есть владельцу, который не читает по-английски,
# в журнале стояло «call» и «meeting». Список закрытый ещё и потому, что это
# значение приходит с экрана и попадает в базу: свободная строка превращает
# журнал в свалку, по которой потом нельзя ни сгруппировать, ни посчитать.
# Стадии воронки — ОДИН список на весь проект. Ключи обязаны совпадать с
# intent_sensor.VALID_STAGES и virtual_layer.STAGE_SIGNAL: мёртвый
# get_crm_pipeline сортировал по 'prospect'/'closed_won'/'closed_lost', каких в
# этой таблице не бывает, — такая «воронка» всегда показывала бы стадии в
# случайном порядке. Порядок здесь — порядок воронки, а не алфавит.
STAGE_LABELS = {
    "lead": "Лид",
    "contact": "Контакт",
    "proposal": "Предложение",
    "negotiation": "Переговоры",
    "won": "Выиграно",
    "lost": "Проиграно",
}


class BadStage(ValueError):
    """Стадия не из списка: ошибка владельца (или клиента), а не сбой сервера."""


ACTIVITY_LABELS = {
    "call": "Звонок",
    "message": "Сообщение",
    "meeting": "Встреча",
    "note": "Заметка",
    "deal": "Сделка",
    "task": "Задача",
}


def activity_label(activity_type: str) -> str:
    """Подпись касания. Неизвестный тип (старые строки) показывается как есть,
    а не прячется: пустая строка в журнале хуже английского слова."""
    return ACTIVITY_LABELS.get(str(activity_type or ""), str(activity_type or ""))


def _coerce_dt(value):
    """None/`datetime` → как есть; ISO-строка → aware datetime; иначе ValueError."""
    if value is None or isinstance(value, _dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"некорректная дата/время: {value!r}")
    return dt.replace(tzinfo=_dt.timezone.utc) if dt.tzinfo is None else dt


def _normalize_ts(data: dict) -> dict:
    """Копия data с приведёнными timestamptz-полями (не мутирует вход)."""
    if not any(f in data for f in _TS_FIELDS):
        return data
    out = dict(data)
    for f in _TS_FIELDS:
        if f in out:
            out[f] = _coerce_dt(out[f])
    return out


class ContactNotFound(LookupError):
    """Контакта с таким id у этого владельца нет.

    Два случая в одном: контакт удалили (в другой вкладке, другим экраном) и
    контакт чужой. Оба — ответ «контакт не найден» (404), а не «внутренняя
    ошибка сервера».
    """


async def _require_contact(pool, owner_id: int, contact_id) -> str:
    """Контакт существует И принадлежит владельцу — иначе ContactNotFound.

    ЧТО БЫЛО. Записи CRM шли в базу без этой проверки. Внешний ключ
    `contact_crm_contact_id_fkey` ловил только НЕСУЩЕСТВУЮЩИЙ id, и ловил его
    пятисоткой: владелец, у которого контакт удалили в другой вкладке,
    нажимал «Сохранить» и получал «внутреннюю ошибку сервера».

    А на ЧУЖОЙ id внешний ключ не срабатывал вовсе — он проверяет только
    наличие контакта в `unified_contacts`, не владельца. То есть по чужому id
    можно было завести строку CRM и журнал активности, привязанные к чужому
    контакту. Прочитать чужое это не давало (чтения скоупятся по owner_id), но
    запись по чужому идентификатору — ровно то, что обязано требовать
    авторизации.
    """
    try:
        key = str(_uuid.UUID(str(contact_id)))
    except (ValueError, AttributeError, TypeError):
        raise ContactNotFound("Контакт не найден")
    ok = await pool.fetchval(
        'SELECT 1 FROM unified_contacts WHERE id=$1::uuid AND owner_id=$2',
        key, owner_id)
    if not ok:
        raise ContactNotFound("Контакт не найден")
    return key


async def get_crm_data(pool, owner_id: int, contact_id: str) -> Optional[dict]:
    row = await pool.fetchrow(
        'SELECT * FROM contact_crm WHERE owner_id=$1 AND contact_id=$2',
        owner_id, contact_id)
    if not row:
        return None
    d = dict(row)
    if isinstance(d.get('custom_fields'), str):
        try:
            d['custom_fields'] = json.loads(d['custom_fields'])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


async def upsert_crm(pool, owner_id: int, contact_id: str, data: dict) -> dict:
    # timestamptz-поля из тела запроса могут прийти строкой → привести к datetime,
    # иначе asyncpg роняет весь upsert (класс #15). ValueError пробросится вызывающему.
    data = _normalize_ts(data)
    # Стадия приходит с экрана в свободной строке: у contact_crm.stage нет ни
    # CHECK, ни enum. Непроверенное значение ложится в базу и тихо выпадает из
    # всего, что о стадиях знает: сигнал виртуального слоя (STAGE_SIGNAL) на
    # него не срабатывает, чип фильтра его не покажет, воронка посчитает его
    # отдельной стадией без подписи.
    if 'stage' in data and data['stage'] is not None:
        if str(data['stage']) not in STAGE_LABELS:
            raise BadStage("Неизвестная стадия сделки")
    await _require_contact(pool, owner_id, contact_id)
    existing = await get_crm_data(pool, owner_id, contact_id)
    if existing:
        sets = []
        params = []
        idx = 1
        for field in ('stage', 'deal_value', 'currency', 'last_interaction_at',
                      'last_interaction_type', 'last_message_preview',
                      'next_reminder_at', 'next_reminder_text', 'assigned_to'):
            if field in data:
                sets.append(f'{field} = ${idx}')
                params.append(data[field])
                idx += 1
        if 'custom_fields' in data:
            sets.append(f'custom_fields = ${idx}::jsonb')
            params.append(json.dumps(data['custom_fields']))
            idx += 1
        if sets:
            sets.append('updated_at = NOW()')
            params.extend([owner_id, contact_id])
            await pool.execute(
                f'UPDATE contact_crm SET {", ".join(sets)} WHERE owner_id=${idx} AND contact_id=${idx+1}',
                *params)
        await _signal_virtual_layer(pool, owner_id, contact_id,
                                    data.get('stage'), existing.get('stage'))
    else:
        await pool.execute(
            '''INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency,
                next_reminder_at, next_reminder_text, custom_fields)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)''',
            owner_id, contact_id,
            data.get('stage', 'lead'),
            data.get('deal_value', 0),
            data.get('currency', 'USD'),
            data.get('next_reminder_at'),
            data.get('next_reminder_text'),
            json.dumps(data.get('custom_fields', {})))
        await _signal_virtual_layer(pool, owner_id, contact_id,
                                    data.get('stage', 'lead'), None)
    return await get_crm_data(pool, owner_id, contact_id)


async def _signal_virtual_layer(pool, owner_id: int, contact_id: str,
                                new_stage, old_stage) -> None:
    """Сообщить виртуальному слою о РУЧНОЙ смене стадии контакта.

    Стадию меняют две двери: сенсор намерений по тексту сообщения и эта —
    экран контакта в мини-аппе. Слою сообщала только первая, поэтому владелец
    перетаскивал человека в «Выиграно», а слой продолжал считать его «Готов
    купить», потом распадом сводил в «Интерес», и человек попадал в список
    «кого дожимать первыми»: дожимать предлагалось того, кто уже заплатил.

    Маппинг один для обеих дверей — `virtual_layer.STAGE_SIGNAL`, иначе
    списки разъедутся, как уже разъехались.

    Fail-open: сбой слоя не ломает сохранение стадии, оно уже применено.
    """
    if not new_stage or new_stage == old_stage:
        return
    try:
        from services import virtual_layer
        sig = virtual_layer.STAGE_SIGNAL.get(str(new_stage))
        if not sig:
            return
        await virtual_layer.signal(
            pool, owner_id, virtual_layer.USER, contact_id, sig,
            confidence=0.8, source="crm_manual")
    except Exception:
        log.debug("crm_engine: virtual_layer signal failed owner=%s stage=%s",
                  owner_id, new_stage, exc_info=True)


async def log_crm_activity(pool, owner_id: int, contact_id: str,
                           activity_type: str, description: str = None,
                           metadata: dict = None) -> None:
    await _require_contact(pool, owner_id, contact_id)
    await pool.execute(
        '''INSERT INTO contact_crm_activity (owner_id, contact_id, activity_type, description, metadata)
           VALUES ($1,$2,$3,$4,$5::jsonb)''',
        owner_id, contact_id, activity_type, description,
        json.dumps(metadata or {}))


async def get_crm_activity(pool, owner_id: int, contact_id: str, limit: int = 30) -> list:
    rows = await pool.fetch(
        '''SELECT * FROM contact_crm_activity
           WHERE owner_id=$1 AND contact_id=$2
           ORDER BY created_at DESC LIMIT $3''',
        owner_id, contact_id, limit)
    out = []
    for r in rows:
        d = dict(r)
        d['activity_label'] = activity_label(d.get('activity_type'))
        out.append(d)
    return out


async def get_upcoming_reminders(pool, owner_id: int, limit: int = 20) -> list:
    rows = await pool.fetch(
        '''SELECT crm.*, uc.first_name, uc.last_name, uc.username
           FROM contact_crm crm
           JOIN unified_contacts uc ON uc.id = crm.contact_id
           WHERE crm.owner_id=$1 AND crm.next_reminder_at IS NOT NULL
           AND crm.next_reminder_at >= NOW()
           ORDER BY crm.next_reminder_at ASC LIMIT $2''',
        owner_id, limit)
    result = []
    for r in rows:
        d = dict(r)
        # username НЕ выбрасываем: он нужен, чтобы написать человеку прямо из
        # напоминания. Раньше он исчезал всегда, когда имя непустое.
        d['contact_name'] = (
            f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip()
            or (d.get('username') and '@' + d['username']) or '?')
        result.append(d)
    return result


async def get_crm_overdue(pool, owner_id: int) -> list:
    rows = await pool.fetch(
        '''SELECT crm.*, uc.first_name, uc.last_name, uc.username
           FROM contact_crm crm
           JOIN unified_contacts uc ON uc.id = crm.contact_id
           WHERE crm.owner_id=$1 AND crm.next_reminder_at < NOW()
           ORDER BY crm.next_reminder_at ASC''',
        owner_id)
    result = []
    for r in rows:
        d = dict(r)
        # username НЕ выбрасываем: он нужен, чтобы написать человеку прямо из
        # напоминания. Раньше он исчезал всегда, когда имя непустое.
        d['contact_name'] = (
            f"{d.pop('first_name', '') or ''} {d.pop('last_name', '') or ''}".strip()
            or (d.get('username') and '@' + d['username']) or '?')
        result.append(d)
    return result


async def pipeline(pool, owner_id: int) -> dict:
    """Сколько контактов в каждой стадии и на какую сумму.

    Экран хаба уже умел ФИЛЬТР по стадии (чипы «Лид», «Предложение», …), но
    ни одного числа рядом: чтобы понять, где лежат сделки, владелец должен был
    по очереди нажать все шесть чипов. Считает это одна функция — та же, что
    отдаёт ручка, — и возвращает ВСЕ стадии, включая пустые: воронка с
    пропущенной серединой читается как «там никого нет и не было».

    Суммы разложены по валютам (`by_currency`). Складывать рубли с долларами в
    одно число нельзя: получилось бы красивое, но неверное «итого».
    """
    rows = await pool.fetch(
        '''SELECT stage, currency, COUNT(*) AS cnt,
                  COALESCE(SUM(deal_value), 0) AS total_value
           FROM contact_crm
           WHERE owner_id = $1
           GROUP BY stage, currency''',
        owner_id)
    acc: dict[str, dict] = {
        key: {'stage': key, 'label': label, 'count': 0, 'by_currency': {}}
        for key, label in STAGE_LABELS.items()
    }
    unknown: dict[str, dict] = {}
    for r in rows:
        key = r['stage'] or 'lead'
        if key in acc:
            item = acc[key]
        else:
            # Стадии вне списка быть не должно (на записи стоит проверка), но
            # старые строки её содержать могут. Прятать нельзя: контакт тогда
            # не виден ни в одной стадии, а в сумме по хабу есть.
            item = unknown.setdefault(
                key, {'stage': key, 'label': key, 'count': 0, 'by_currency': {}})
        item['count'] += int(r['cnt'] or 0)
        cur = (r['currency'] or 'USD').upper()
        val = float(r['total_value'] or 0)
        if val:
            item['by_currency'][cur] = item['by_currency'].get(cur, 0.0) + val
    overdue = await pool.fetchval(
        "SELECT COUNT(*) FROM contact_crm "
        "WHERE owner_id = $1 AND next_reminder_at IS NOT NULL "
        "AND next_reminder_at <= NOW() + INTERVAL '7 days'",
        owner_id)
    stages = list(acc.values()) + list(unknown.values())
    return {
        'stages': stages,
        'total': sum(s['count'] for s in stages),
        'reminders_7d': int(overdue or 0),
    }


# Здесь лежали get_crm_pipeline и get_crm_stats — две мёртвые воронки. Первая
# сортировала по стадиям, каких в contact_crm не бывает ('prospect',
# 'closed_won', 'closed_lost'), то есть её «порядок воронки» был случайным;
# вторая считала то же самое без порядка вообще. Ни одну не звал никто, кроме
# тестов. Осталась одна, pipeline(), и её зовёт ручка GET /uch/crm/pipeline.


# ЧЕТЫРЕ ДУБЛЯ УДАЛЕНЫ ОТСЮДА (22.10.2026). Все четыре не звал никто, кроме
# тестов, и каждый был хуже своего живого двойника ровно тем, чего не делал:
#   * create_crm_activity — писал в журнал БЕЗ `_require_contact`, то есть по
#     чужому contact_id, и выглядел удобнее log_crm_activity (возвращал id);
#   * get_crm_activities — читал журнал БЕЗ русских подписей, в отличие от
#     get_crm_activity;
#   * update_crm_stage — не нормализовал поля и не поднимал сигнал
#     виртуального слоя при смене стадии, в отличие от upsert_crm;
#   * get_crm_reminders — копия get_upcoming_reminders.
# Живой агент, подключающий CRM к экрану, выбирает по имени и подписи, а не по
# тому, кого уже зовут: такой дубль возвращает дыру или теряет перевод.
# Проверка «они не вернулись» — tests/test_touch_journal_has_a_door.py.
