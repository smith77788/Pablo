"""
Invite Distribution Engine — умное распределение инвайт-ссылок.

Логика:
- Каждая ссылка ограничена по числу вступлений (Telegram limit ~infinity для обычных, но аккаунт может получить флуд)
- Ротация ссылок по аккаунтам: каждый аккаунт генерирует свою ссылку
- Балансировка по нагрузке и trust_score аккаунтов
- Кэш ссылок с TTL (ссылки не перегенерируются каждый раз)
- Отслеживание использований для ротации
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

_LINK_TTL_SECONDS = 3600 * 6   # ссылка актуальна 6 часов
_MAX_USES_PER_LINK = 50         # мягкий лимит использований до ротации

# In-memory кэш: (owner_id, channel_id) -> [(account_id, link, generated_at, use_count)]
_link_cache: dict[tuple[int, int], list[dict]] = {}
_cache_lock = asyncio.Lock()


async def get_invite_links(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
    count: int = 3,
) -> list[dict]:
    """
    Получить N актуальных инвайт-ссылок для канала из разных аккаунтов.
    Возвращает список {'account_id', 'link', 'use_count'}.
    Автоматически генерирует если кэш устарел или пуст.
    """
    async with _cache_lock:
        cached = _link_cache.get((owner_id, channel_id), [])
        now = time.time()
        # Отфильтровать устаревшие и перегруженные
        fresh = [
            e for e in cached
            if (now - e["generated_at"]) < _LINK_TTL_SECONDS
            and e["use_count"] < _MAX_USES_PER_LINK
        ]
        if len(fresh) >= count:
            # Сортировать по наименьшему использованию
            return sorted(fresh, key=lambda x: x["use_count"])[:count]

    # Нужно сгенерировать новые ссылки
    new_links = await _generate_links(pool, owner_id, channel_id, count)
    async with _cache_lock:
        existing = _link_cache.get((owner_id, channel_id), [])
        # Убрать устаревшие из кэша
        now = time.time()
        existing = [
            e for e in existing
            if (now - e["generated_at"]) < _LINK_TTL_SECONDS
            and e["use_count"] < _MAX_USES_PER_LINK
        ]
        # Добавить новые, убрав дубли по account_id
        existing_acc_ids = {e["account_id"] for e in existing}
        for nl in new_links:
            if nl["account_id"] not in existing_acc_ids:
                existing.append(nl)
                existing_acc_ids.add(nl["account_id"])
        _link_cache[(owner_id, channel_id)] = existing

    return sorted(existing, key=lambda x: x["use_count"])[:count]


async def _generate_links(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
    count: int,
) -> list[dict]:
    """Генерирует новые инвайт-ссылки через разные аккаунты-администраторы."""
    from services import account_manager

    # Аккаунты-администраторы канала (владелец, высокий trust_score)
    accounts = await pool.fetch(
        """SELECT a.id, a.session_str, a.device_model, a.system_version, a.app_version
           FROM tg_accounts a
           JOIN managed_channels mc ON mc.acc_id = a.id AND mc.channel_id = $1
           WHERE a.owner_id = $2 AND a.is_active = true
           ORDER BY a.trust_score DESC NULLS LAST
           LIMIT $3""",
        channel_id, owner_id, count * 2,
    )

    results = []
    for acc in accounts:
        if len(results) >= count:
            break
        try:
            link = await account_manager.get_channel_invite_link(
                acc["session_str"], channel_id, _acc=dict(acc)
            )
            if link:
                results.append({
                    "account_id": acc["id"],
                    "link": link,
                    "generated_at": time.time(),
                    "use_count": 0,
                })
        except Exception as e:
            log.debug("invite_engine: acc %d gen error: %s", acc["id"], e)

    return results


def record_link_use(owner_id: int, channel_id: int, link: str) -> None:
    """Зафиксировать использование ссылки (увеличить use_count)."""
    cached = _link_cache.get((owner_id, channel_id), [])
    for entry in cached:
        if entry["link"] == link:
            entry["use_count"] += 1
            break


def invalidate_channel(owner_id: int, channel_id: int) -> None:
    """Сбросить кэш ссылок для канала (при смене конфига)."""
    _link_cache.pop((owner_id, channel_id), None)


async def get_best_link(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
) -> Optional[str]:
    """Вернуть одну лучшую (наименее используемую) ссылку."""
    links = await get_invite_links(pool, owner_id, channel_id, count=1)
    if links:
        link = links[0]["link"]
        record_link_use(owner_id, channel_id, link)
        return link
    return None


async def get_distributed_links(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
    recipient_count: int,
) -> list[str]:
    """
    Вернуть список ссылок для N получателей — каждая ссылка по очереди,
    балансировка по нагрузке.
    """
    if recipient_count <= 0:
        return []

    # Получить достаточно ссылок
    pool_size = min(5, max(1, recipient_count // 20 + 1))
    links_pool = await get_invite_links(pool, owner_id, channel_id, count=pool_size)
    if not links_pool:
        return []

    result = []
    for i in range(recipient_count):
        entry = links_pool[i % len(links_pool)]
        result.append(entry["link"])
        entry["use_count"] += 1

    return result
