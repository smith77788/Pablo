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


def _parse_operators(query: str) -> ParsedQuery:
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
    return parsed


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

def _build_operator_conditions(parsed: ParsedQuery, param_offset: int) -> tuple[list[str], list[Any], int]:
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

async def search_contacts(
    pool,
    owner_id: int,
    query: str,
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    parsed = _parse_operators(query)
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

    all_conditions: list[str] = [f'owner_id = $1']
    params: list[Any] = [owner_id]
    idx = 2

    op_conditions, op_params, idx = _build_operator_conditions(parsed, idx)
    all_conditions.extend(op_conditions)
    params.extend(op_params)

    if tokens:
        token_where, token_params = _build_search_conditions(tokens, idx)
        if token_where:
            all_conditions.append(f'({token_where})')
            params.extend(token_params)
            idx += len(token_params)

    where_clause = ' AND '.join(all_conditions)

    if has_operators and tokens:
        match_count_expr, new_idx = _build_match_count_expression(tokens, idx)
        idx = new_idx
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, ({match_count_expr}) as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY match_score DESC, first_name
                LIMIT ${idx - 1} OFFSET ${idx}''',
            *params)
    elif has_operators:
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, 0 as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY first_name
                LIMIT ${idx - 1} OFFSET ${idx}''',
            *params)
    elif len(tokens) == 1:
        q = f'%{tokens[0]}%'
        params.append(q)
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, 0 as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY
                    CASE WHEN first_name ILIKE ${idx} THEN 0
                         WHEN username ILIKE ${idx} THEN 1
                         WHEN display_name ILIKE ${idx} THEN 2
                         WHEN company ILIKE ${idx} THEN 3
                         ELSE 4 END,
                    first_name
                LIMIT ${idx + 1} OFFSET ${idx + 2}''',
            *params)
    else:
        match_count_expr, new_idx = _build_match_count_expression(tokens, idx)
        idx = new_idx
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, ({match_count_expr}) as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY match_score DESC, first_name
                LIMIT ${idx - 1} OFFSET ${idx}''',
            *params)

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
    parsed = _parse_operators(query)
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

    op_conditions, op_params, idx = _build_operator_conditions(parsed, idx)
    all_conditions.extend(op_conditions)
    params.extend(op_params)

    q = f'%{query.strip().lower()}%'

    if not has_operators:
        all_conditions.append(
            f'''(
                first_name ILIKE ${idx} OR last_name ILIKE ${idx} OR
                username ILIKE ${idx} OR display_name ILIKE ${idx} OR
                CAST(telegram_user_id AS TEXT) ILIKE ${idx} OR
                company ILIKE ${idx} OR notes ILIKE ${idx} OR
                $${idx} = ANY(tags)
            )'''
        )
        params.append(q)
        idx += 1

    where_clause = ' AND '.join(all_conditions)

    params.append(limit)
    rank_limit = idx
    params.append(999999)
    rank_limit2 = idx + 1

    rows = await pool.fetch(
        f'''SELECT *,
            CASE
                WHEN first_name ILIKE ${idx} THEN 0
                WHEN username ILIKE ${idx} THEN 1
                WHEN display_name ILIKE ${idx} THEN 2
                WHEN $${idx} = ANY(tags) THEN 3
                WHEN company ILIKE ${idx} THEN 4
                ELSE 5
            END as rank
           FROM unified_contacts
           WHERE {where_clause}
           ORDER BY rank, first_name
           LIMIT ${rank_limit2}''',
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
    parsed = _parse_operators(query)
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

    all_conditions: list[str] = [f'owner_id = $1']
    params: list[Any] = [owner_id]
    idx = 2

    op_conditions, op_params, idx = _build_operator_conditions(parsed, idx)
    all_conditions.extend(op_conditions)
    params.extend(op_params)

    if tokens:
        token_where, token_params = _build_search_conditions(tokens, idx)
        if token_where:
            all_conditions.append(f'({token_where})')
            params.extend(token_params)
            idx += len(token_params)

    where_clause = ' AND '.join(all_conditions)

    count_params = list(params)
    count_row = await pool.fetchrow(
        f'SELECT COUNT(*) as cnt FROM unified_contacts WHERE {where_clause}',
        *count_params)
    total = count_row['cnt'] if count_row else 0

    if has_operators and tokens:
        match_count_expr, new_idx = _build_match_count_expression(tokens, idx)
        idx = new_idx
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, ({match_count_expr}) as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY match_score DESC, first_name
                LIMIT ${idx - 1} OFFSET ${idx}''',
            *params)
    elif len(tokens) == 1:
        params.append(f'%{tokens[0]}%')
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, 0 as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY
                    CASE WHEN first_name ILIKE ${idx} THEN 0
                         WHEN username ILIKE ${idx} THEN 1
                         WHEN display_name ILIKE ${idx} THEN 2
                         WHEN company ILIKE ${idx} THEN 3
                         ELSE 4 END,
                    first_name
                LIMIT ${idx + 1} OFFSET ${idx + 2}''',
            *params)
    else:
        match_count_expr, new_idx = _build_match_count_expression(tokens, idx)
        idx = new_idx
        params.append(limit)
        params.append(offset)
        rows = await pool.fetch(
            f'''SELECT *, ({match_count_expr}) as match_score FROM unified_contacts
                WHERE {where_clause}
                ORDER BY match_score DESC, first_name
                LIMIT ${idx - 1} OFFSET ${idx}''',
            *params)

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
