"""
Geo Router — гео-осведомлённый выбор аккаунтов для операций.

Логика:
- У каждого прокси есть geo_country из proxy_manager
- Привязанный к прокси аккаунт = аккаунт из региона прокси
- Операции могут требовать аккаунт из конкретной страны/региона
- Балансировка нагрузки по гео-пулам
"""

from __future__ import annotations

import logging
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


async def get_accounts_by_geo(
    pool: asyncpg.Pool,
    owner_id: int,
    country_code: str,
    limit: int = 5,
) -> list[dict]:
    """
    Вернуть аккаунты из заданной страны (по гео прокси).
    country_code: двухбуквенный ISO код (RU, US, DE, ...)
    """
    rows = await pool.fetch(
        """SELECT a.id, a.session_str, a.phone, a.first_name,
                  a.device_model, a.system_version, a.app_version,
                  a.lang_code, a.system_lang_code, a.proxy_id,
                  p.proxy_url, p.geo_country
           FROM tg_accounts a
           JOIN user_proxies p ON p.id = a.proxy_id
           WHERE a.owner_id = $1
             AND a.is_active = true
             AND a.session_str IS NOT NULL
             AND UPPER(p.geo_country) = UPPER($2)
           ORDER BY a.trust_score DESC NULLS LAST
           LIMIT $3""",
        owner_id,
        country_code,
        limit,
    )
    return [dict(r) for r in rows]


async def get_geo_distribution(
    pool: asyncpg.Pool,
    owner_id: int,
) -> dict[str, int]:
    """
    Вернуть словарь {country_code: count} — распределение активных аккаунтов по странам.
    """
    rows = await pool.fetch(
        """SELECT COALESCE(UPPER(p.geo_country), 'UNKNOWN') AS country,
                  COUNT(a.id) AS cnt
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id
           WHERE a.owner_id = $1 AND a.is_active = true
           GROUP BY country
           ORDER BY cnt DESC""",
        owner_id,
    )
    return {r["country"]: r["cnt"] for r in rows}


async def get_best_account_for_region(
    pool: asyncpg.Pool,
    owner_id: int,
    country_code: Optional[str] = None,
) -> Optional[dict]:
    """
    Выбрать лучший аккаунт для региона (или любой, если country_code=None).
    Критерии: trust_score DESC, flood_count_7d ASC.
    """
    if country_code:
        accounts = await get_accounts_by_geo(pool, owner_id, country_code, limit=1)
        if accounts:
            return accounts[0]

    # Fallback — любой аккаунт через flood_engine (учитывает in-memory risk scoring)
    from services.flood_engine import get_best_account

    return await get_best_account(pool, owner_id, action_type="geo_action")


async def get_geo_aware_pool(
    pool: asyncpg.Pool,
    owner_id: int,
    target_countries: list[str],
    per_country: int = 2,
) -> list[dict]:
    """
    Вернуть пул аккаунтов, покрывающий список стран.
    Для каждой страны — до per_country аккаунтов.
    """
    result = []
    seen_ids: set[int] = set()

    for country in target_countries:
        accs = await get_accounts_by_geo(pool, owner_id, country, limit=per_country)
        for acc in accs:
            if acc["id"] not in seen_ids:
                result.append(acc)
                seen_ids.add(acc["id"])

    return result
