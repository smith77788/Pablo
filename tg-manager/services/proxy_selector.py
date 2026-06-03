"""
Proxy Selector — унифицированный выбор и оценка прокси.

В системе BotMother прокси всегда привязаны к аккаунту (a.proxy_id).
Этот модуль предоставляет:
  - Получение score прокси из infra_memory
  - Запись результатов работы прокси в infra_memory
  - Ранжирование аккаунтов с учётом качества их прокси
  - Быстрая проверка здоровья прокси из in-memory состояния
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import asyncpg

from services import infra_memory

log = logging.getLogger(__name__)


def get_proxy_score(proxy_url: str, action_type: str = "default") -> float:
    """Качество прокси по опыту infra_memory. 0.5 = нейтральный/новый.

    Значения:
      > 0.7  — хороший прокси, низкий процент ошибок
      0.5    — нет данных, нейтральная оценка
      < 0.3  — проблемный прокси, высокий процент ошибок / высокая латентность
    """
    return infra_memory.get_proxy_score(proxy_url, action_type)


def record_proxy_result(
    proxy_url: str,
    action_type: str,
    success: bool,
    latency_ms: float = 0.0,
) -> None:
    """Записать результат работы прокси в infra_memory (non-blocking, in-memory)."""
    if not proxy_url:
        return
    infra_memory.record_proxy_op(proxy_url, action_type, success, latency_ms=latency_ms)


async def rank_accounts_by_proxy_quality(
    accounts: list[dict],
    action_type: str = "default",
) -> list[dict]:
    """Переранжировать список аккаунтов с учётом качества прокси из infra_memory.

    Аккаунты без прокси или с нейтральным score (0.5) идут последними среди равных.
    Возвращает новый список — исходный не модифицируется.
    """
    if not accounts:
        return []

    def _sort_key(acc: dict) -> float:
        proxy_url = acc.get("proxy_url") or ""
        proxy_score = get_proxy_score(proxy_url, action_type) if proxy_url else 0.4
        trust = acc.get("trust_score") or 0.5
        return -(trust * 0.6 + proxy_score * 0.4)

    return sorted(accounts, key=_sort_key)


async def get_healthy_proxies(
    pool: asyncpg.Pool,
    owner_id: int,
    action_type: str = "default",
    min_score: float = 0.3,
) -> list[dict]:
    """Вернуть список прокси с достаточным quality score для owner_id.

    Каждый элемент: {proxy_url, geo_country, score, is_active}
    """
    try:
        rows = await pool.fetch(
            """SELECT p.proxy_url, p.geo_country, p.is_active
               FROM user_proxies p
               WHERE p.owner_id=$1 AND p.is_active=TRUE
               ORDER BY p.id""",
            owner_id,
        )
    except Exception as e:
        log.warning("proxy_selector.get_healthy_proxies failed owner=%d: %s", owner_id, e)
        return []

    result = []
    for row in rows:
        proxy_url = row["proxy_url"] or ""
        score = get_proxy_score(proxy_url, action_type)
        if score >= min_score:
            result.append({
                "proxy_url": proxy_url,
                "geo_country": row.get("geo_country", ""),
                "is_active": row.get("is_active", True),
                "score": score,
            })

    result.sort(key=lambda p: -p["score"])
    return result


async def check_proxy_health(proxy_url: str, action_type: str = "default") -> dict:
    """Вернуть сводку состояния конкретного прокси.

    Возвращает: {proxy_url, score, status: 'good'|'degraded'|'bad'|'unknown'}
    """
    score = get_proxy_score(proxy_url, action_type)
    if score >= 0.65:
        status = "good"
    elif score >= 0.4:
        status = "degraded"
    elif score > 0:
        status = "bad"
    else:
        status = "unknown"
    return {"proxy_url": proxy_url, "score": score, "status": status}
