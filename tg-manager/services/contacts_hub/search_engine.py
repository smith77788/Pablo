from __future__ import annotations
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Generator, Optional

import asyncpg

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT = 50
_MAX_CACHE_SIZE = 256
_CACHE_TTL_SECONDS = 300
_FUZZY_THRESHOLD = 0.6
_LEVENSHTEIN_MAX_DISTANCE = 3
_PAGINATION_DEFAULT_LIMIT = 20
_PAGINATION_MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Search operators parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedQuery:
    free_text: list[str] = field(default_factory=list)
    username: Optional[str] = None
    tag: Optional[str] = None
    company: Optional[str] = None
    phone: Optional[str] = None


_OPERATOR_RE = re.compile(
    r'@(?P<username>\S+)'
    r'|#(?P<tag>\S+)'
    r'|company:(?P<company>\S+)'
    r'|phone:(?P<phone>\S+)',
    re.IGNORECASE,
)


def parse_operators(query: str) -> tuple[ParsedQuery, str]:
    """Операторы запроса и остаток строки — ОДИН разбор на бота и на хаб.

    Операторы (`@ник`, `#тег`, `company:`, `phone:`) понимал только поиск
    бота; список мини-аппа собирал условие своим кодом и о них не знал, то
    есть набранный в единственном поле поиска `#vip` не находил никого.
    Теперь разбор здесь, а условия строит `build_operator_conditions` — обе
    стороны зовут одно и то же, иначе «в боте ищет, в приложении нет»
    вернётся на первой же правке.

    Остаток строки отдаётся СЫРЫМ: список бьёт его на слова своим правилом
    (у него проходят и односимвольные), и менять ему матчинг заодно с
    операторами значит менять две вещи одной правкой.
    """
    parsed = ParsedQuery()
    remaining = query

    for m in _OPERATOR_RE.finditer(query):
        if m.group('username'):
            parsed.username = m.group('username').lower()
        elif m.group('tag'):
            parsed.tag = m.group('tag').lower()
        elif m.group('company'):
            parsed.company = m.group('company').lower()
        elif m.group('phone'):
            parsed.phone = m.group('phone').lower()

    remaining = _OPERATOR_RE.sub('', remaining).strip()
    parsed.free_text = _tokenize(remaining)
    return parsed, remaining



# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def _tokenize(query: str) -> list[str]:
    tokens = re.split(r'\s+', query.strip().lower())
    return [t for t in tokens if len(t) >= 2]


# ---------------------------------------------------------------------------
# Levenshtein distance (pure Python, no deps)
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev_row = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr_row = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr_row.append(
                min(prev_row[j + 1] + 1, curr_row[j] + 1, prev_row[j] + cost)
            )
        prev_row = curr_row
    return prev_row[-1]


# ---------------------------------------------------------------------------
# Fuzzy similarity helpers
# ---------------------------------------------------------------------------

def _fuzzy_score(needle: str, haystack: str) -> float:
    needle_l = needle.lower()
    haystack_l = haystack.lower()

    if needle_l in haystack_l:
        return 1.0

    seq_ratio = SequenceMatcher(None, needle_l, haystack_l).ratio()

    max_dist = min(_LEVENSHTEIN_MAX_DISTANCE, len(needle_l))
    lev_dist = _levenshtein(needle_l, haystack_l)
    if lev_dist <= max_dist:
        lev_score = 1.0 - (lev_dist / max(len(needle_l), len(haystack_l)))
    else:
        lev_score = 0.0

    return max(seq_ratio, lev_score)


def _fuzzy_matches_token(token: str, field_value: Optional[str]) -> bool:
    if not field_value:
        return False
    return _fuzzy_score(token, field_value) >= _FUZZY_THRESHOLD


# ---------------------------------------------------------------------------
# Simple in-memory LRU cache with TTL
# ---------------------------------------------------------------------------

class _SearchCache:
    def __init__(self, max_size: int = _MAX_CACHE_SIZE, ttl: int = _CACHE_TTL_SECONDS):
        self._max_size = max_size
        self._ttl = ttl
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, value = self._store[key]
            if time.monotonic() - ts < self._ttl:
                self._store.move_to_end(key)
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        if key in self._store:
            del self._store[key]
        elif len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()


_cache = _SearchCache()


# ---------------------------------------------------------------------------
# SQL builders
# ---------------------------------------------------------------------------

def _page(limit: Any, offset: Any) -> tuple[int, int]:
    """Потолок и сдвиг страницы: мусор, отрицательные и «всю базу» — к разумным
    значениям. Иначе `LIMIT -5` — ошибка запроса, а `limit=abc` — пятисотка ещё
    до обращения к базе."""
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = _DEFAULT_LIMIT
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    return max(1, min(lim, _PAGINATION_MAX_LIMIT)), max(0, min(off, 10_000_000))


def build_operator_conditions(parsed: ParsedQuery, param_offset: int) -> tuple[list[str], list[Any], int]:
    conditions: list[str] = []
    params: list[Any] = []
    idx = param_offset

    if parsed.username:
        conditions.append(f'username ILIKE ${idx}')
        params.append(f'%{parsed.username}%')
        idx += 1

    if parsed.tag:
        conditions.append(f'${idx} = ANY(tags)')
        params.append(parsed.tag)
        idx += 1

    if parsed.company:
        conditions.append(f'company ILIKE ${idx}')
        params.append(f'%{parsed.company}%')
        idx += 1

    if parsed.phone:
        conditions.append(
            f'(CAST(telegram_user_id AS TEXT) ILIKE ${idx} OR '
            f'EXISTS (SELECT 1 FROM contact_sources cs '
            f'WHERE cs.contact_id = unified_contacts.id AND cs.raw_data::text ILIKE ${idx}))'
        )
        params.append(f'%{parsed.phone}%')
        idx += 1

    return conditions, params, idx


def _build_search_conditions(tokens: list[str], param_offset: int) -> tuple[str, list[Any]]:
    if not tokens:
        return '', []

    conditions: list[str] = []
    params: list[Any] = []
    idx = param_offset

    for token in tokens:
        q = f'%{token}%'
        token_conditions = (
            f'(first_name ILIKE ${idx} OR last_name ILIKE ${idx} OR '
            f'username ILIKE ${idx} OR display_name ILIKE ${idx} OR '
            f'CAST(telegram_user_id AS TEXT) ILIKE ${idx} OR '
            f'company ILIKE ${idx} OR notes ILIKE ${idx} OR '
            f'position ILIKE ${idx} OR '
            f'${idx} = ANY(tags))'
        )
        conditions.append(token_conditions)
        params.append(q)
        idx += 1

    where = ' AND '.join(conditions)
    return where, params


def _build_ranking(param_offset: int) -> str:
    idx = param_offset
    parts = []
    for _ in range(10):
        parts.append(
            f'(CASE WHEN first_name ILIKE ${idx} THEN 0 '
            f'WHEN username ILIKE ${idx} THEN 1 '
            f'WHEN display_name ILIKE ${idx} THEN 2 '
            f'WHEN company ILIKE ${idx} THEN 3 '
            f'ELSE 4 END)'
        )
        idx += 1
    return ' + '.join(parts)


def _build_match_count_expression(tokens: list[str], param_offset: int) -> tuple[str, int]:
    idx = param_offset
    parts: list[str] = []
    for _ in tokens:
        parts.append(
            f'(CASE WHEN first_name ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN last_name ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN username ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN display_name ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN company ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN position ILIKE ${idx} THEN 1 ELSE 0 END + '
            f'CASE WHEN notes ILIKE ${idx} THEN 1 ELSE 0 END)'
        )
        idx += 1
    return ' + '.join(parts), idx


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def _build_search_query(
    parsed: ParsedQuery, tokens: list[str], owner_id: int, limit: int, offset: int,
) -> tuple[str, list[Any], str, list[Any]]:
    """Запрос страницы поиска: (SQL, параметры, WHERE, параметры WHERE).

    ОДНА арифметика плейсхолдеров на весь движок. Раньше её повторяли семь
    почти одинаковых ветвей в двух функциях, и в шести из них она была сломана:
    ранжирование (`match_score`) строилось от индекса ПОСЛЕ слов запроса, то
    есть ссылалось на плейсхолдеры `limit`/`offset` («first_name ILIKE
    <число>»), а `LIMIT ${idx - 1} OFFSET ${idx}` брали на один меньше реальных
    позиций — в `LIMIT` уезжала строка `%слово%`. Postgres отвечал «argument of
    LIMIT must be type bigint, not type text», и поиск по контактам падал
    пятисоткой на любом запросе из двух слов и на любом запросе с оператором.

    Правила, которые держит эта функция:
    * ранжирование ПЕРЕИСПОЛЬЗУЕТ плейсхолдеры слов (`tokens_at`), своих не
      заводит;
    * номера страницы считаются от фактической длины списка параметров — это и
      есть единственная правда о том, что уедет в запрос.
    """
    all_conditions: list[str] = ['owner_id = $1']
    params: list[Any] = [owner_id]
    idx = 2

    op_conditions, op_params, idx = build_operator_conditions(parsed, idx)
    all_conditions.extend(op_conditions)
    params.extend(op_params)

    tokens_at = idx
    if tokens:
        token_where, token_params = _build_search_conditions(tokens, tokens_at)
        if token_where:
            all_conditions.append(f'({token_where})')
            params.extend(token_params)

    where_clause = ' AND '.join(all_conditions)
    where_params = list(params)

    if tokens:
        match_count_expr, _unused = _build_match_count_expression(tokens, tokens_at)
        score_expr = f'({match_count_expr})'
    else:
        score_expr = '0'

    if len(tokens) == 1:
        # одно слово: порядок задаёт поле, в котором совпало
        order_by = (f'CASE WHEN first_name ILIKE ${tokens_at} THEN 0 '
                    f'WHEN username ILIKE ${tokens_at} THEN 1 '
                    f'WHEN display_name ILIKE ${tokens_at} THEN 2 '
                    f'WHEN company ILIKE ${tokens_at} THEN 3 '
                    f'ELSE 4 END, first_name')
    elif tokens:
        order_by = 'match_score DESC, first_name'
    else:
        order_by = 'first_name'

    lim_idx = len(params) + 1
    params.append(limit)
    params.append(offset)
    sql = (f'SELECT *, {score_expr} as match_score FROM unified_contacts\n'
           f'            WHERE {where_clause}\n'
           f'            ORDER BY {order_by}\n'
           f'            LIMIT ${lim_idx} OFFSET ${lim_idx + 1}')
    return sql, params, where_clause, where_params


async def search_contacts(
    pool,
    owner_id: int,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    limit, offset = _page(limit, offset)
    parsed = parse_operators(query)[0]
    has_operators = any([parsed.username, parsed.tag, parsed.company, parsed.phone])

    tokens = parsed.free_text

    if not tokens and not has_operators:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name LIMIT $2 OFFSET $3',
            owner_id, limit, offset)
        from services.contacts_hub.repository import _parse_json_fields
        return [_parse_json_fields(dict(r)) for r in rows]

    cache_key = f'search:{owner_id}:{query}:{limit}:{offset}'
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    sql, params, _where, _wp = _build_search_query(
        parsed, tokens, owner_id, limit, offset)
    rows = await pool.fetch(sql, *params)

    from services.contacts_hub.repository import _parse_json_fields
    result = [_parse_json_fields(dict(r)) for r in rows]
    _cache.set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Spotlight search (fast, for autocomplete / type-ahead)
# ---------------------------------------------------------------------------

async def search_contacts_spotlight(
    pool,
    owner_id: int,
    query: str,
    limit: int = 30,
) -> list[dict]:
    parsed = parse_operators(query)[0]
    tokens = parsed.free_text
    has_operators = any([parsed.username, parsed.tag, parsed.company, parsed.phone])

    if not tokens and not has_operators:
        return []

    cache_key = f'spotlight:{owner_id}:{query}:{limit}'
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    all_conditions: list[str] = [f'owner_id = $1']
    params: list[Any] = [owner_id]
    idx = 2

    op_conditions, op_params, idx = build_operator_conditions(parsed, idx)
    all_conditions.extend(op_conditions)
    params.extend(op_params)

    q = f'%{query.strip().lower()}%'
    bare = query.strip().lower()

    # Шаблон поиска и сам запрос нужны И в условии, И в ранжировании (CASE), в
    # том числе когда условие собрано по операторам, — поэтому параметры
    # заводятся всегда, а не внутри ветки.
    pat_idx = idx
    params.append(q)
    idx += 1
    tag_idx = idx
    params.append(bare)
    idx += 1

    if not has_operators:
        all_conditions.append(
            f'''(
                first_name ILIKE ${pat_idx} OR last_name ILIKE ${pat_idx} OR
                username ILIKE ${pat_idx} OR display_name ILIKE ${pat_idx} OR
                CAST(telegram_user_id AS TEXT) ILIKE ${pat_idx} OR
                company ILIKE ${pat_idx} OR notes ILIKE ${pat_idx} OR
                ${tag_idx} = ANY(tags)
            )'''
        )

    where_clause = ' AND '.join(all_conditions)

    # Пул кандидатов для питоновского ранжирования: `limit` отдаётся наружу
    # после сортировки по combined_score, поэтому выбрать нужно больше, чем
    # вернём, — но не всю таблицу (раньше стояло 999999, то есть «прочитать
    # все контакты владельца на каждое нажатие в поиске»).
    cand_idx = idx
    params.append(min(max(int(limit) * 10, 100), 1000))

    rows = await pool.fetch(
        f'''SELECT *,
            CASE
                WHEN first_name ILIKE ${pat_idx} THEN 0
                WHEN username ILIKE ${pat_idx} THEN 1
                WHEN display_name ILIKE ${pat_idx} THEN 2
                WHEN ${tag_idx} = ANY(tags) THEN 3
                WHEN company ILIKE ${pat_idx} THEN 4
                ELSE 5
            END as rank
           FROM unified_contacts
           WHERE {where_clause}
           ORDER BY rank, first_name
           LIMIT ${cand_idx}''',
        *params)

    seen: set[int] = set()
    results: list[dict] = []
    for r in rows:
        cid = r['id']
        if cid in seen:
            continue
        seen.add(cid)
        d = dict(r)

        if tokens:
            text = (
                f"{d.get('first_name', '')} {d.get('last_name', '')} "
                f"{d.get('username', '')} {d.get('display_name', '')} "
                f"{d.get('company', '')} {d.get('position', '')} "
                f"{' '.join(d.get('tags', []) or [])}"
            ).lower()
            exact_matches = sum(1 for t in tokens if t in text)
            d['match_score'] = exact_matches / len(tokens) if tokens else 0.0

            fuzzy_bonus = 0.0
            for t in tokens:
                for field_val in (d.get('first_name', ''), d.get('last_name', ''),
                                 d.get('username', ''), d.get('display_name', ''),
                                 d.get('company', '')):
                    if field_val and _fuzzy_score(t, field_val) >= _FUZZY_THRESHOLD:
                        fuzzy_bonus += 0.1
            d['fuzzy_score'] = min(fuzzy_bonus, 0.5)

            d['combined_score'] = d['match_score'] + d['fuzzy_score']
        else:
            d['match_score'] = 0.0
            d['fuzzy_score'] = 0.0
            d['combined_score'] = 0.0

        results.append(d)

    if tokens:
        results.sort(key=lambda x: (-x.get('combined_score', 0), x.get('rank', 5), x.get('first_name', '')))
    else:
        results.sort(key=lambda x: (x.get('rank', 5), x.get('first_name', '')))

    result = results[:limit]
    _cache.set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Advanced search with full control (new API)
# ---------------------------------------------------------------------------

async def search_contacts_advanced(
    pool,
    owner_id: int,
    query: str,
    *,
    limit: int = _PAGINATION_DEFAULT_LIMIT,
    offset: int = 0,
    fuzzy: bool = True,
    fuzzy_threshold: float = _FUZZY_THRESHOLD,
) -> dict:
    limit, offset = _page(limit, offset)
    parsed = parse_operators(query)[0]
    tokens = parsed.free_text
    has_operators = any([parsed.username, parsed.tag, parsed.company, parsed.phone])

    if not tokens and not has_operators:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name LIMIT $2 OFFSET $3',
            owner_id, limit, offset)
        count_row = await pool.fetchrow(
            'SELECT COUNT(*) as cnt FROM unified_contacts WHERE owner_id=$1', owner_id)
        total = count_row['cnt'] if count_row else 0
        return {
            'items': [dict(r) for r in rows],
            'total': total,
            'offset': offset,
            'limit': limit,
            'has_more': offset + limit < total,
        }

    cache_key = f'advanced:{owner_id}:{query}:{limit}:{offset}:{fuzzy}:{fuzzy_threshold}'
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    sql, params, where_clause, where_params = _build_search_query(
        parsed, tokens, owner_id, limit, offset)

    count_row = await pool.fetchrow(
        f'SELECT COUNT(*) as cnt FROM unified_contacts WHERE {where_clause}',
        *where_params)
    total = count_row['cnt'] if count_row else 0

    rows = await pool.fetch(sql, *params)

    items = [dict(r) for r in rows]

    if fuzzy and tokens:
        fuzzy_extra = await _fuzzy_search(pool, owner_id, tokens, fuzzy_threshold, exclude_ids={r['id'] for r in items})
        items.extend(fuzzy_extra)
        items.sort(key=lambda x: (-x.get('match_score', 0), x.get('first_name', '')))
        items = items[:limit]

    result = {
        'items': items,
        'total': total,
        'offset': offset,
        'limit': limit,
        'has_more': offset + limit < total,
    }
    _cache.set(cache_key, result)
    return result


async def _fuzzy_search(
    pool,
    owner_id: int,
    tokens: list[str],
    threshold: float,
    exclude_ids: set[int],
) -> list[dict]:
    all_rows = await pool.fetch(
        'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name',
        owner_id)

    results: list[dict] = []
    for r in all_rows:
        if r['id'] in exclude_ids:
            continue
        d = dict(r)
        text_fields = [
            d.get('first_name', '') or '',
            d.get('last_name', '') or '',
            d.get('username', '') or '',
            d.get('display_name', '') or '',
            d.get('company', '') or '',
            d.get('position', '') or '',
        ]
        combined_text = ' '.join(text_fields).lower()

        best_score = 0.0
        for t in tokens:
            for field_val in text_fields:
                if field_val:
                    score = _fuzzy_score(t, field_val.lower())
                    best_score = max(best_score, score)
            if t in combined_text:
                best_score = max(best_score, 1.0)

        if best_score >= threshold:
            d['match_score'] = round(best_score * 10, 2)
            results.append(d)

    results.sort(key=lambda x: (-x.get('match_score', 0), x.get('first_name', '')))
    return results[:10]


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def clear_search_cache() -> None:
    _cache.clear()
