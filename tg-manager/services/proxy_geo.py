"""Определение страны/города прокси по его РЕАЛЬНОМУ exit-IP.

Единый источник правды для гео прокси: и бот (ручная кнопка «определить гео»),
и мини-апп (авто-определение при добавлении прокси) зовут одну функцию. Раньше
логика жила только в bot/handlers/proxy_manager.py, и мини-апп, добавляя прокси,
оставлял geo_country пустым — из-за чего гео-подбор аккаунтов/прокси и проверка
гео-бана ботов не видели страну.

Страна берётся по ответу ip-api.com, ЗАПРОШЕННОМУ ЧЕРЕЗ сам прокси: так гео
отражает фактический exit-IP, а не хост из URL (у hostname-прокси это разные
вещи). Пишем ISO2 (UA/DE/RU) — матч-сайты сравнивают geo_country как ISO2.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any, cast

log = logging.getLogger(__name__)

_IP_API_URL = "http://ip-api.com/json/?fields=country,countryCode,city,query"


async def detect_proxy_geo(proxy_url: str, *, timeout: float = 8.0) -> dict:
    """Вернуть {geo_country (ISO2), geo_city} для прокси, или {} при сбое.

    Работает и для прокси с авторизацией, и без неё. Никогда не бросает: гео —
    необязательное обогащение, и его сбой не должен ронять «добавить прокси»
    (без прокси в продукте не работает ничего).
    """
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return {}
    try:
        import aiohttp

        socks_module = importlib.import_module("aiohttp_socks")
        ProxyConnector = cast(Any, getattr(socks_module, "ProxyConnector"))

        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                _IP_API_URL, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("detect_proxy_geo: не удалось определить гео прокси")
        return {}

    try:
        from services.geo_normalize import to_iso2
        return {
            "geo_country": to_iso2(data.get("countryCode") or data.get("country")),
            "geo_city": data.get("city"),
        }
    except Exception:
        log.debug("detect_proxy_geo: нормализация гео не удалась")
        return {}
