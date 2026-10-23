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


def _as_list(v) -> list:
    """Список из колонки jsonb, которая приехала строкой.

    НАХОДКА. asyncpg отдаёт jsonb СТРОКОЙ (кодек json на пуле нигде не
    ставится), а сравнение дублей делало `set(contact['phones'])` — то есть
    превращало строку `'["+79990000001"]'` в набор СИМВОЛОВ. У любых двух
    контактов с телефонами пересекались `+`, `7`, `9`, `"`, `[`, и движок
    ставил причину `same_phone` (+0.3) паре с совершенно разными номерами.
    Вместе с одинаковым именем (0.15) и компанией (0.1) это 0.55, то есть выше
    порога: экран «Дубликаты» показывал посторонних людей как одного человека и
    писал «один телефон» — неправду, — а кнопка «Объединить» рядом удаляет
    вторую запись. Для порога пакетного слияния (0.9) ложного признака не
    хватает, поэтому массового разрушения не было, но одно нажатие — было.
    """
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def compute_merge_confidence(contact_a: dict, contact_b: dict) -> dict:
    score = 0.0
    reasons = []

    if contact_a.get('telegram_user_id') and contact_a['telegram_user_id'] == contact_b.get('telegram_user_id'):
        score += 0.5
        reasons.append('same_telegram_id')

    phones_a = {str(x) for x in _as_list(contact_a.get('phones')) if x}
    phones_b = {str(x) for x in _as_list(contact_b.get('phones')) if x}
    if phones_a and phones_b and phones_a & phones_b:
        # 0.4, а не 0.3. Пока телефоны сравнивались как наборы символов, вес
        # не имел значения: признак стоял почти на каждой паре. Когда признак
        # стал настоящим, стало видно, что его вес слишком мал: ОДИН И ТОТ ЖЕ
        # номер плюс одинаковое имя давали 0.45, то есть настоящий двойник
        # оставался ниже порога 0.5 и на экран не попадал. Номер опознаёт
        # человека не хуже ника (0.25) — скорее лучше. 0.4 держит и обратное:
        # общий номер САМ ПО СЕБЕ (семейный, рабочий) порога не берёт.
        score += 0.4
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


# Названия совпадений ПО-РУССКИ. Экран дублей печатал `dp.reasons.join(', ')`,
# то есть владелец читал `same_telegram_id, same_phone` — коды из кода. Карта
# живёт на сервере по той же причине, что ACTIVITY_LABELS и STAGE_LABELS: две
# карты (своя на экране и своя в боте) расходятся молча, а правило «владелец
# читает только по-русски» нарушает любая из них.
REASON_LABELS = {
    'same_telegram_id': 'один Telegram ID',
    'same_phone': 'один телефон',
    'same_username': 'один @ник',
    'same_name': 'одинаковое имя',
    'same_first_name': 'одинаковое имя (без фамилии)',
    'same_company': 'одна компания',
}


def reason_labels(reasons) -> list:
    """Коды совпадений -> подписи. Неизвестный код отдаётся как есть: лучше
    незнакомое слово, чем пропавшая причина."""
    return [REASON_LABELS.get(r, str(r)) for r in (reasons or [])]


# Потолок группы кандидатов. Группа — контакты с ОДНИМ ником, телефоном или
# telegram_id; две-три записи там норма. Тысяча — это не дубликаты, а мусорное
# значение (служебный номер, '0', один ник у пачки импортированных записей), и
# квадратичность вернулась бы внутри такой группы. Срезанное НЕ молчит: сколько
# групп не сравнивалось, уходит наружу вместе с результатом.
_DUP_GROUP_CAP = 300

async def detect_smart_duplicates(pool, owner_id: int) -> dict:
    """Пары-кандидаты на слияние: {'duplicates': [...], 'skipped_groups': n}.

    БЫЛО: читались ВСЕ контакты владельца и сравнивались ВСЕ пары —
    n*(n-1)/2 вызовов compute_merge_confidence в одном запросе. На 10 000
    контактах это ~50 млн сравнений на каждое открытие экрана дублей, то есть
    экран не «медленный», а недоступный: запрос не возвращается, и владелец
    видит ошибку вместо списка.

    ПОЧЕМУ ОТБОР ТОЧНЫЙ, А НЕ ПРИБЛИЗИТЕЛЬНЫЙ. Порог 0.5 и веса из
    compute_merge_confidence: без совпадения telegram_user_id (0.5), телефона
    (0.3) или ника (0.25) максимум даёт имя (0.15) плюс компания (0.1) = 0.25.
    Значит КАЖДАЯ пара с confidence >= 0.5 обязана совпасть хотя бы по одному
    из этих трёх полей, и сравнение только внутри таких групп даёт тот же
    результат, а не похожий. Это не рассуждение в комментарии: тест
    test_duplicate_detection_is_exact_not_approximate перебирает формы пары и
    краснеет, если добавленный признак сломает оценку сверху.

    Возвращается dict, а не список: сколько групп не сравнивалось — часть
    ответа. Молча срезанный список дублей читается как «больше дублей нет».
    """
    contacts = await pool.fetch(
        '''SELECT id, telegram_user_id, username, first_name, last_name, phones, company
           FROM unified_contacts WHERE owner_id=$1''', owner_id)
    rows = [dict(c) for c in contacts]

    groups: dict[tuple, list[int]] = {}
    for i, c in enumerate(rows):
        keys = set()
        if c.get('telegram_user_id'):
            keys.add(('tg', str(c['telegram_user_id'])))
        if c.get('username'):
            keys.add(('un', str(c['username']).lower()))
        phones = c.get('phones') or []
        if isinstance(phones, str):
            try:
                phones = json.loads(phones)
            except Exception:
                phones = []
        for ph in (phones or []):
            if ph:
                keys.add(('ph', str(ph)))
        for k in keys:
            groups.setdefault(k, []).append(i)

    duplicates = []
    seen_pairs = set()
    skipped_groups = 0
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        if len(idxs) > _DUP_GROUP_CAP:
            skipped_groups += 1
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                a, b = rows[idxs[x]], rows[idxs[y]]
                key = tuple(sorted([str(a['id']), str(b['id'])]))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                result = compute_merge_confidence(a, b)
                if result['confidence'] < 0.5:
                    continue
                duplicates.append({
                    'contact_a': {'id': a['id'], 'name': f"{a.get('first_name') or ''} {a.get('last_name') or ''}".strip() or a.get('username') or '?'},
                    'contact_b': {'id': b['id'], 'name': f"{b.get('first_name') or ''} {b.get('last_name') or ''}".strip() or b.get('username') or '?'},
                    'confidence': result['confidence'],
                    'reasons': result['reasons'],
                    'reason_labels': reason_labels(result['reasons']),
                })

    duplicates.sort(key=lambda x: x['confidence'], reverse=True)
    return {'duplicates': duplicates, 'skipped_groups': skipped_groups}


async def update_trust_scores(pool, owner_id: int) -> int:
    """Пересчитать доверие по всем контактам владельца за три запроса.

    Раньше здесь было ТРИ запроса НА КАЖДЫЙ контакт (источники, сам контакт
    заново, UPDATE) плюс один общий: на хабе в десять тысяч контактов — за
    тридцать тысяч обращений к базе, то есть пересчёт, который нельзя
    запустить из приложения. Теперь: контакты, их источники и одна запись
    пачкой. Формула (compute_trust_score) не тронута, и ей по-прежнему
    передаются настоящие строки источников, а не их число, — иначе правка
    формулы молча разошлась бы с этим путём.
    """
    contacts = await pool.fetch(
        'SELECT * FROM unified_contacts WHERE owner_id=$1', owner_id)
    if not contacts:
        return 0
    sources = await pool.fetch(
        'SELECT s.* FROM contact_sources s '
        'JOIN unified_contacts c ON c.id = s.contact_id '
        'WHERE c.owner_id=$1', owner_id)
    by_contact: dict = {}
    for s in sources:
        by_contact.setdefault(s['contact_id'], []).append(dict(s))

    # `unified_contacts.id` — UUID, а не число. Пачка писалась с
    # `$1::bigint[]`, то есть Postgres отбивал её целиком («operator does not
    # exist: uuid = bigint»), и пересчёт доверия МОЛЧА не делал ничего:
    # владелец видел нули. Тип ключа здесь обязан совпадать со схемой.
    ids: list = []
    scores: list[float] = []
    for c in contacts:
        ids.append(c['id'])
        scores.append(compute_trust_score(dict(c), by_contact.get(c['id'], [])))
    # owner_id в условии остаётся: id пришли из выборки по владельцу, но
    # запись без этой проверки — чужая строка при любой будущей ошибке выше.
    await pool.execute(
        'UPDATE unified_contacts AS u SET trust_score = v.score '
        'FROM (SELECT * FROM unnest($1::uuid[], $2::double precision[]) '
        '      AS t(id, score)) AS v '
        'WHERE u.id = v.id AND u.owner_id = $3',
        ids, scores, owner_id)
    return len(ids)


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


# УДАЛЕНО: compute_merge_confidence_improved / detect_smart_duplicates_improved.
# Параллельная реализация того же правила с суффиксом «improved», на которую не
# ссылался никто, кроме тестов. Опасна именно удобством: агент, подключающий
# движок к экрану, выбирает функцию по имени и подписи, и «улучшенная» выглядит
# предпочтительнее той, которую зовёт продукт. Её лишние признаки (source_type,
# partial_name_overlap) сломали бы и доказательство точности отбора по группам
# выше: оценку сверху даёт набор весов compute_merge_confidence, а не вообще
# любой. Нужны эти признаки — добавлять их в единственную реализацию и
# перепроверять тест точности.

# УДАЛЕНО: get_conflict_resolution_suggestions(pool, conflict_id). Советы по
# разрешению конфликта полей — без owner_id ВООБЩЕ: conflict_id это обычное
# целое, и дверь к этой функции сразу стала бы перебором чужих конфликтов.
# Двери у неё не было, звали только тесты. Понадобится — писать заново со
# скоупом владельца в запросе, а не в обработчике.

