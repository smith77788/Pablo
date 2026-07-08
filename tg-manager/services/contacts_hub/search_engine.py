from __future__ import annotations
import logging
import re
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


def _tokenize(query: str) -> list:
    tokens = re.split(r'\s+', query.strip().lower())
    return [t for t in tokens if len(t) >= 2]


def _build_search_conditions(tokens: list, param_offset: int) -> tuple:
    if not tokens:
        return '', []

    conditions = []
    params = []
    idx = param_offset

    for token in tokens:
        q = f'%{token}%'
        token_conditions = (
            f'(first_name ILIKE ${idx} OR last_name ILIKE ${idx} OR '
            f'username ILIKE ${idx} OR display_name ILIKE ${idx} OR '
            f'CAST(telegram_user_id AS TEXT) ILIKE ${idx} OR '
            f'company ILIKE ${idx} OR notes ILIKE ${idx} OR '
            f'position ILIKE ${idx} OR email ILIKE ${idx} OR '
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


async def search_contacts(pool, owner_id: int, query: str, limit: int = 50) -> list:
    tokens = _tokenize(query)

    if not tokens:
        rows = await pool.fetch(
            'SELECT * FROM unified_contacts WHERE owner_id=$1 ORDER BY first_name LIMIT $2',
            owner_id, limit)
        return [dict(r) for r in rows]

    if len(tokens) == 1:
        q = f'%{tokens[0]}%'
        rows = await pool.fetch(
            '''SELECT *, 0 as search_rank FROM unified_contacts
               WHERE owner_id = $1 AND (
                   first_name ILIKE $2 OR last_name ILIKE $2 OR
                   username ILIKE $2 OR display_name ILIKE $2 OR
                   CAST(telegram_user_id AS TEXT) ILIKE $2 OR
                   company ILIKE $2 OR notes ILIKE $2 OR
                   position ILIKE $2 OR email ILIKE $2 OR
                   $2 = ANY(tags)
               )
               ORDER BY
                   CASE WHEN first_name ILIKE $2 THEN 0
                        WHEN username ILIKE $2 THEN 1
                        WHEN display_name ILIKE $2 THEN 2
                        WHEN company ILIKE $2 THEN 3
                        ELSE 4 END,
                   first_name
               LIMIT $3''',
            owner_id, q, limit)
        return [dict(r) for r in rows]

    where_clause, params = _build_search_conditions(tokens, 3)
    count_params = params.copy()
    idx = 3 + len(params)

    rows = await pool.fetch(
        f'''SELECT *, 0 as search_rank FROM unified_contacts
            WHERE owner_id = $1 AND {where_clause}
            ORDER BY first_name
            LIMIT ${idx}''',
        owner_id, *params, limit)

    return [dict(r) for r in rows]


async def search_contacts_spotlight(pool, owner_id: int, query: str, limit: int = 30) -> list:
    tokens = _tokenize(query)
    if not tokens:
        return []

    q = f'%{query.strip()}%'
    rows = await pool.fetch(
        '''SELECT *,
            CASE
                WHEN first_name ILIKE $2 THEN 0
                WHEN username ILIKE $2 THEN 1
                WHEN display_name ILIKE $2 THEN 2
                WHEN $2 = ANY(tags) THEN 3
                WHEN company ILIKE $2 THEN 4
                ELSE 5
            END as rank
           FROM unified_contacts
           WHERE owner_id = $1 AND (
               first_name ILIKE $2 OR last_name ILIKE $2 OR
               username ILIKE $2 OR display_name ILIKE $2 OR
               CAST(telegram_user_id AS TEXT) ILIKE $2 OR
               company ILIKE $2 OR notes ILIKE $2 OR
               $2 = ANY(tags)
           )
           ORDER BY rank, first_name
           LIMIT $3''',
        owner_id, q, limit)

    if len(tokens) > 1:
        results = []
        seen = set()
        for r in rows:
            cid = r['id']
            if cid in seen:
                continue
            seen.add(cid)
            d = dict(r)
            text = f"{d.get('first_name', '')} {d.get('last_name', '')} {d.get('username', '')} {d.get('company', '')} {' '.join(d.get('tags', []))}".lower()
            match_count = sum(1 for t in tokens if t in text)
            d['match_score'] = match_count / len(tokens)
            results.append(d)
        results.sort(key=lambda x: (-x['match_score'], x.get('rank', 5)))
        return results[:limit]

    return [dict(r) for r in rows]
