"""
Proxy Selector — унифицированный выбор и оценка прокси.

В системе BotMother прокси всегда привязаны к аккаунту (a.proxy_id).
Этот модуль предоставляет:
  - Получение score прокси из infra_memory
  - Запись результатов работы прокси в infra_memory
  - Ранжирование аккаунтов с учётом качества их прокси
  - Быстрая проверка здоровья прокси из in-memory состояния
  - IP diversity validation для предотвращения datacenter-банов
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Optional

import asyncpg

from services import infra_memory

log = logging.getLogger(__name__)

# Known datacenter IP ranges (Railway, Render, AWS, DigitalOcean, etc.)
_DATACENTER_RANGES = [
    # Railway.app
    (ipaddress.ip_network("10.0.0.0/8"), "Railway"),
    (ipaddress.ip_network("172.16.0.0/12"), "Railway/Cloud"),
    # Render
    (ipaddress.ip_network("45.33.0.0/16"), "Render"),
    # AWS
    (ipaddress.ip_network("52.0.0.0/8"), "AWS"),
    (ipaddress.ip_network("54.0.0.0/8"), "AWS"),
    # DigitalOcean
    (ipaddress.ip_network("64.227.0.0/16"), "DigitalOcean"),
    (ipaddress.ip_network("159.89.0.0/16"), "DigitalOcean"),
    # Hetzner
    (ipaddress.ip_network("168.119.0.0/16"), "Hetzner"),
    # Linode
    (ipaddress.ip_network("45.79.0.0/16"), "Linode"),
    # Google Cloud
    (ipaddress.ip_network("34.0.0.0/8"), "Google Cloud"),
    # Azure
    (ipaddress.ip_network("40.0.0.0/8"), "Azure"),
    # Vultr
    (ipaddress.ip_network("45.76.0.0/16"), "Vultr"),
    # OVH
    (ipaddress.ip_network("51.0.0.0/8"), "OVH"),
]


def extract_ip_from_proxy(proxy_url: str) -> Optional[str]:
    """Extract IP address from proxy URL."""
    if not proxy_url:
        return None
    # Try to extract IP from socks5://user:pass@host:port format
    match = re.search(r'@((\[?[0-9a-fA-F.:]+\]?)|(\d+\.\d+\.\d+\.\d+)):', proxy_url)
    if match:
        return match.group(1).strip("[]")
    return None


def is_datacenter_ip(ip_str: str) -> tuple[bool, str]:
    """Check if IP is a known datacenter IP. Returns (is_datacenter, provider)."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network, provider in _DATACENTER_RANGES:
            if ip in network:
                return True, provider
    except ValueError:
        pass
    return False, ""


def validate_ip_diversity(accounts: list[dict], max_per_ip: int = 3) -> dict:
    """Validate that accounts don't share too many IPs.
    
    Returns: {
        'valid': bool,
        'warnings': list[str],
        'ip_usage': {ip: [account_ids]},
        'datacenter_warnings': list[str]
    }
    """
    ip_to_accounts: dict[str, list[int]] = {}
    datacenter_accounts: list[tuple[int, str]] = []  # (account_id, provider)
    
    for acc in accounts:
        ip = extract_ip_from_proxy(acc.get("proxy_url", ""))
        if not ip:
            continue
            
        if ip not in ip_to_accounts:
            ip_to_accounts[ip] = []
        ip_to_accounts[ip].append(acc.get("id", 0))
        
        # Check for datacenter IPs
        is_dc, provider = is_datacenter_ip(ip)
        if is_dc:
            datacenter_accounts.append((acc.get("id", 0), provider))
    
    # Check violations
    warnings = []
    for ip, acc_ids in ip_to_accounts.items():
        if len(acc_ids) > max_per_ip:
            warnings.append(
                f"IP {ip} используется {len(acc_ids)} аккаунтами (> {max_per_ip}). "
                f"Риск datacenter-бана!"
            )
    
    datacenter_warnings = [
        f"Аккаунт {acc_id} на datacenter IP ({provider}). "
        f"Рекомендуется residential прокси."
        for acc_id, provider in datacenter_accounts
    ]
    
    return {
        "valid": len(warnings) == 0,
        "warnings": warnings,
        "ip_usage": ip_to_accounts,
        "datacenter_warnings": datacenter_warnings,
        "datacenter_count": len(datacenter_accounts),
        "total_accounts_checked": len(accounts),
    }


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
        log.warning(
            "proxy_selector.get_healthy_proxies failed owner=%d: %s", owner_id, e
        )
        return []

    result = []
    for row in rows:
        proxy_url = row["proxy_url"] or ""
        score = get_proxy_score(proxy_url, action_type)
        if score >= min_score:
            result.append(
                {
                    "proxy_url": proxy_url,
                    "geo_country": row.get("geo_country", ""),
                    "is_active": row.get("is_active", True),
                    "score": score,
                }
            )

    result.sort(key=lambda p: -p["score"])
    return result


async def check_proxy_health(proxy_url: str, action_type: str = "default") -> dict:
    """Вернуть сводку состояния конкретного прокси.

    Если in-memory score нейтральный (новый прокси без истории операций),
    выполняет реальную проверку подключения к api.telegram.org.

    Возвращает: {proxy_url, score, status: 'good'|'degraded'|'bad'|'unknown', latency_ms?}
    """
    score = get_proxy_score(proxy_url, action_type)
    latency_ms: int | None = None

    # Neutral score (0.5) means no history — do a real connectivity test
    if score == 0.5 and proxy_url:
        try:
            import time as _time
            import aiohttp
            import importlib as _il

            socks_module = _il.import_module("aiohttp_socks")
            ProxyConnector = getattr(socks_module, "ProxyConnector")
            connector = ProxyConnector.from_url(proxy_url)
            t0 = _time.monotonic()
            async with aiohttp.ClientSession(connector=connector) as _sess:
                async with _sess.get(
                    "https://api.telegram.org",
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False,
                ) as resp:
                    latency_ms = int((_time.monotonic() - t0) * 1000)
                    alive = resp.status < 500
            # Record real result in infra_memory so future calls use it
            record_proxy_result(proxy_url, action_type, alive, latency_ms=float(latency_ms))
            score = get_proxy_score(proxy_url, action_type)
        except Exception:
            record_proxy_result(proxy_url, action_type, False)
            score = get_proxy_score(proxy_url, action_type)

    if score >= 0.65:
        status = "good"
    elif score >= 0.4:
        status = "degraded"
    elif score > 0:
        status = "bad"
    else:
        status = "unknown"

    result: dict = {"proxy_url": proxy_url, "score": score, "status": status}
    if latency_ms is not None:
        result["latency_ms"] = latency_ms
    return result
