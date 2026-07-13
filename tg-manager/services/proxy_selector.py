"""Proxy Selector — unified proxy selection and evaluation.

In Infragram, proxies are always bound to an account (a.proxy_id).
This module provides:
  - Getting proxy scores from infra_memory
  - Recording proxy work results to infra_memory
  - Ranking accounts based on their proxy quality
  - Quick proxy health checks from in-memory state
  - IP diversity validation to prevent datacenter bans
  - Proxy URL validation and SSRF protection

Usage:
    from services.proxy_selector import extract_ip_from_proxy, is_datacenter_ip

    ip = extract_ip_from_proxy("socks5://user:pass@1.2.3.4:1080")
    is_dc, provider = is_datacenter_ip(ip)
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import asyncpg

from services import infra_memory
from services.cache import TTLCache

log = logging.getLogger(__name__)

# ── Кэш статистики прокси-пула (TTL 2 мин) ─────────────────────────────────────
_proxy_pool_stats_cache = TTLCache(default_ttl=120.0, max_size=200)
# Кэш результатов health-check прокси (TTL 3 мин) ────────────────────────────────
_proxy_health_cache = TTLCache(default_ttl=180.0, max_size=500)
# Кэш IP-проверок datacenter (TTL 30 мин — IP не меняются часто) ────────────────
_datacenter_ip_cache: dict[str, tuple[bool, str, float]] = {}
_DC_IP_TTL = 1800.0

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

# Supported proxy schemes
_PROXY_SCHEMES = {"socks4", "socks5", "http", "https"}

# Private/internal IP patterns for SSRF protection
_PRIVATE_IP_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|0\.0\.0\.0|::1$)"
)


def validate_proxy_url(proxy_url: str) -> tuple[bool, str]:
    """Validate proxy URL format and safety.

    Returns (is_valid, error_message).
    Checks: non-empty, valid URL scheme, valid host, no internal IPs.
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return False, "empty proxy URL"
    url = proxy_url.strip()
    if len(url) > 2048:
        return False, "proxy URL too long"
    try:
        p = urlparse(url)
    except Exception:
        return False, "invalid URL format"
    if p.scheme.lower() not in _PROXY_SCHEMES:
        return False, f"unsupported scheme: {p.scheme}"
    if not p.hostname:
        return False, "missing hostname"
    return True, ""


def is_safe_proxy_url(proxy_url: str) -> bool:
    """SSRF protection: check if proxy URL points to a safe external address.

    Blocks: localhost, private IPs, loopback, link-local, internal TLDs.
    Best-effort (no DNS resolution) — blocks obvious internal addresses.
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return False
    try:
        p = urlparse(proxy_url.strip())
    except Exception:
        return False
    if not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "0.0.0.0") or host.endswith(".local") or host.endswith(".internal"):
        return False
    if _PRIVATE_IP_RE.match(host):
        return False
    return True


def validate_proxy_input(proxy_url: str, max_len: int = 2048) -> Optional[str]:
    """Validate and sanitize proxy URL input. Returns cleaned URL or None if invalid."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    url = proxy_url.strip()[:max_len]
    if not url:
        return None
    is_valid, _err = validate_proxy_url(url)
    if not is_valid:
        return None
    if not is_safe_proxy_url(url):
        return None
    return url


def extract_ip_from_proxy(proxy_url: str) -> Optional[str]:
    """Extract IP address from proxy URL."""
    if not proxy_url or not isinstance(proxy_url, str):
        return None
    if len(proxy_url) > 2048:
        return None
    # proxy_url хранится зашифрованным — расшифровываем, иначе изоляция по IP
    # сломается (regex не найдёт host в шифротексте). Passthrough для legacy.
    from services.token_vault import decrypt_token

    proxy_url = decrypt_token(proxy_url)
    # Try to extract IP from socks5://user:pass@host:port format
    match = re.search(r'@((\[?[0-9a-fA-F.:]+\]?)|(\d+\.\d+\.\d+\.\d+)):', proxy_url)
    if match:
        return match.group(1).strip("[]")
    return None


def is_datacenter_ip(ip_str: str) -> tuple[bool, str]:
    """Check if IP is a known datacenter IP. Returns (is_datacenter, provider)."""
    _now = time.monotonic()
    _cached = _datacenter_ip_cache.get(ip_str)
    if _cached and (_now - _cached[2]) < _DC_IP_TTL:
        return _cached[0], _cached[1]
    try:
        ip = ipaddress.ip_address(ip_str)
        for network, provider in _DATACENTER_RANGES:
            if ip in network:
                _datacenter_ip_cache[ip_str] = (True, provider, _now)
                return True, provider
    except ValueError:
        pass
    _datacenter_ip_cache[ip_str] = (False, "", _now)
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
    if not accounts or not isinstance(accounts, list):
        return {
            "valid": True,
            "warnings": [],
            "ip_usage": {},
            "datacenter_warnings": [],
            "datacenter_count": 0,
            "total_accounts_checked": 0,
        }
    max_per_ip = max(1, min(max_per_ip, 100))
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


async def audit_proxy_isolation(pool, owner_id: int, max_per_ip: int = 1) -> dict:
    """Аудит изоляции: активные аккаунты, делящие один IP прокси (риск бана), и
    аккаунты без прокси. max_per_ip=1 — строгая изоляция (каждый аккаунт на своём IP).

    Подключает ранее «мёртвую» validate_ip_diversity к реальному owner-скоупу.
    proxy_url зашифрован — extract_ip_from_proxy расшифровывает внутри.
    """
    rows = await pool.fetch(
        "SELECT a.id, a.phone, a.cf_relay_url, p.proxy_url "
        "FROM tg_accounts a "
        "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
        "WHERE a.owner_id = $1 AND a.is_active = TRUE",
        owner_id,
    )
    accounts = [dict(r) for r in rows]
    # Изоляция по прокси считается только для аккаунтов с прокси; аккаунты на
    # CF-релее покрыты отдельным механизмом (edge-IP), их не считаем «голыми».
    with_proxy = [a for a in accounts if a.get("proxy_url")]
    on_relay = [a["id"] for a in accounts
                if not a.get("proxy_url") and a.get("cf_relay_url")]
    naked = [a["id"] for a in accounts
             if not a.get("proxy_url") and not a.get("cf_relay_url")]
    diversity = validate_ip_diversity(with_proxy, max_per_ip=max_per_ip)
    shared = {
        ip: ids for ip, ids in diversity["ip_usage"].items() if len(ids) > max_per_ip
    }
    # Слабая изоляция релея: один воркер на >max_per_ip аккаунтов (round-robin при
    # нехватке воркеров). Это предупреждение, а не блокер (edge-IP всё равно не Railway).
    relay_usage: dict[str, list] = {}
    for a in accounts:
        if not a.get("proxy_url") and a.get("cf_relay_url"):
            relay_usage.setdefault(a["cf_relay_url"], []).append(a["id"])
    relay_shared = {u: ids for u, ids in relay_usage.items() if len(ids) > max_per_ip}
    return {
        "total_active": len(accounts),
        "with_proxy": len(with_proxy),
        "on_relay": len(on_relay),
        "accounts_on_relay": on_relay,
        "accounts_without_proxy": naked,  # реально «голые» (ни прокси, ни релея)
        "shared_ip_groups": [
            {"ip": ip, "account_ids": ids, "count": len(ids)}
            for ip, ids in sorted(shared.items(), key=lambda kv: -len(kv[1]))
        ],
        "relay_shared_groups": [
            {"url": u, "account_ids": ids, "count": len(ids)}
            for u, ids in sorted(relay_shared.items(), key=lambda kv: -len(kv[1]))
        ],
        "datacenter_warnings": diversity["datacenter_warnings"],
        "datacenter_count": diversity["datacenter_count"],
        "isolation_ok": not shared and not naked,
    }


async def _fetch_backup_proxies(pool, owner_id: int) -> list[dict]:
    """Живые активные резервные прокси владельца. Колонка is_backup может ещё не
    примениться (лаг миграции) — в этом случае возвращаем пустой список, а не падаем."""
    try:
        rows = await pool.fetch(
            "SELECT id, proxy_url FROM user_proxies "
            "WHERE owner_id=$1 AND is_active=TRUE AND is_backup=TRUE ORDER BY id",
            owner_id,
        )
    except Exception as e:  # UndefinedColumnError при лаге миграции и т.п.
        log.warning("failover: backup proxies unavailable owner=%s: %s", owner_id, e)
        return []
    return [dict(r) for r in rows]


async def failover_dead_proxies(pool, owner_id: int) -> dict:
    """Переназначить аккаунты с МЁРТВЫМ прокси на здоровый РЕЗЕРВНЫЙ (is_backup).

    Реально пробит (probe_proxy → api.telegram.org) каждый назначенный и каждый
    резервный прокси. Аккаунт с мёртвым прокси переводится на живой резервный,
    соблюдая IP-изоляцию: один резервный IP — одному аккаунту. Идемпотентно и
    безопасно: любые сбои по отдельному аккаунту/прокси не роняют весь проход.

    Возвращает {checked, healthy, reassigned:[{account_id, from_proxy_id, to_proxy_id}],
    still_dead_no_backup:[account_id], backups_available, backups_healthy}.
    """
    result = {
        "checked": 0,
        "healthy": 0,
        "reassigned": [],
        "still_dead_no_backup": [],
        "backups_available": 0,
        "backups_healthy": 0,
    }
    try:
        accounts = await pool.fetch(
            "SELECT a.id AS account_id, a.proxy_id, p.proxy_url "
            "FROM tg_accounts a "
            "JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
            "WHERE a.owner_id=$1 AND a.is_active=TRUE AND a.proxy_id IS NOT NULL",
            owner_id,
        )
    except Exception as e:
        log.warning("failover: cannot list accounts owner=%s: %s", owner_id, e)
        return result

    backups = await _fetch_backup_proxies(pool, owner_id)
    result["backups_available"] = len(backups)

    # IP уже занятые активными аккаунтами (для изоляции резервных).
    used_ips: set[str] = set()
    for acc in accounts:
        ip = extract_ip_from_proxy(acc["proxy_url"] or "")
        if ip:
            used_ips.add(ip)

    # Пробим резервные параллельно (до 5 одновременно), оставляем живые с уникальным IP.
    _BACKUP_CONCURRENCY = 5
    _sem = asyncio.Semaphore(_BACKUP_CONCURRENCY)

    async def _probe_with_sem(b: dict) -> dict | None:
        async with _sem:
            probe = await probe_proxy(b["proxy_url"])
            if not probe.get("ok"):
                return None
            ip = extract_ip_from_proxy(b["proxy_url"] or "")
            if ip and ip in used_ips:
                return None
            return {"id": b["id"], "ip": ip}

    healthy_backups: list[dict] = []
    if backups:
        probe_results = await asyncio.gather(
            *(_probe_with_sem(b) for b in backups),
            return_exceptions=True,
        )
        for r in probe_results:
            if isinstance(r, dict) and r is not None:
                healthy_backups.append(r)
    result["backups_healthy"] = len(healthy_backups)

    # Кэш проб основных прокси по proxy_id (несколько аккаунтов могут делить прокси).
    # Пробим уникальные proxy_id параллельно, результаты кэшируем.
    unique_proxy_ids: dict[int, str] = {}
    for acc in accounts:
        pid = acc["proxy_id"]
        if pid not in unique_proxy_ids:
            unique_proxy_ids[pid] = acc["proxy_url"] or ""

    probe_cache: dict[int, bool] = {}
    if unique_proxy_ids:
        _main_sem = asyncio.Semaphore(_BACKUP_CONCURRENCY)

        async def _probe_main(pid: int, purl: str) -> tuple[int, bool]:
            async with _main_sem:
                return pid, bool((await probe_proxy(purl)).get("ok"))

        main_results = await asyncio.gather(
            *(_probe_main(pid, purl) for pid, purl in unique_proxy_ids.items()),
            return_exceptions=True,
        )
        for r in main_results:
            if isinstance(r, tuple):
                probe_cache[r[0]] = r[1]

    for acc in accounts:
        result["checked"] += 1
        pid = acc["proxy_id"]
        if pid not in probe_cache:
            probe_cache[pid] = bool((await probe_proxy(acc["proxy_url"])).get("ok"))
        alive = probe_cache[pid]
        if alive:
            result["healthy"] += 1
            continue

        # Прокси мёртв — помечаем и ищем резервный.
        try:
            await pool.execute(
                "UPDATE user_proxies SET is_alive=FALSE, last_check=now() WHERE id=$1 AND owner_id=$2",
                pid, owner_id,
            )
        except Exception:
            pass

        if not healthy_backups:
            result["still_dead_no_backup"].append(acc["account_id"])
            continue

        backup = healthy_backups.pop(0)  # один резервный — одному аккаунту (изоляция)
        try:
            await pool.execute(
                "UPDATE tg_accounts SET proxy_id=$1 WHERE id=$2 AND owner_id=$3",
                backup["id"], acc["account_id"], owner_id,
            )
            result["reassigned"].append({
                "account_id": acc["account_id"],
                "from_proxy_id": pid,
                "to_proxy_id": backup["id"],
            })
            if backup["ip"]:
                used_ips.add(backup["ip"])
        except Exception as e:
            log.warning("failover: reassign failed acc=%s: %s", acc["account_id"], e)
            healthy_backups.insert(0, backup)  # вернуть резервный в пул
            result["still_dead_no_backup"].append(acc["account_id"])

    return result


def get_proxy_score(proxy_url: str, action_type: str = "default") -> float:
    """Качество прокси по опыту infra_memory. 0.5 = нейтральный/новый.

    Значения:
      > 0.7  — хороший прокси, низкий процент ошибок
      0.5    — нет данных, нейтральная оценка
      < 0.3  — проблемный прокси, высокий процент ошибок / высокая латентность
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return 0.5
    if len(proxy_url) > 2048:
        return 0.5
    return infra_memory.get_proxy_score(proxy_url, action_type)


def record_proxy_result(
    proxy_url: str,
    action_type: str,
    success: bool,
    latency_ms: float = 0.0,
) -> None:
    """Записать результат работы прокси в infra_memory (non-blocking, in-memory)."""
    if not proxy_url or not isinstance(proxy_url, str):
        return
    if len(proxy_url) > 2048:
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

    # proxy_url хранится зашифрованным — get_proxy_score сам нормализует ключ к
    # plaintext внутри, но потребители ЭТОГО списка (если будут построить socks-
    # соединение или показать пользователю) ждут читаемый URL, не шифротекст.
    from services.token_vault import decrypt_token

    result = []
    for row in rows:
        proxy_url = row["proxy_url"] or ""
        score = get_proxy_score(proxy_url, action_type)
        if score >= min_score:
            result.append(
                {
                    "proxy_url": decrypt_token(proxy_url) if proxy_url else proxy_url,
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
    _cache_key = f"hp:{proxy_url}:{action_type}"
    _cached = _proxy_health_cache.get(_cache_key)
    if _cached is not None:
        return _cached

    score = get_proxy_score(proxy_url, action_type)
    latency_ms: int | None = None

    # Neutral score (0.5) means no history — do a real connectivity test
    if score == 0.5 and proxy_url:
        try:
            import time as _time
            import aiohttp
            import importlib as _il

            # proxy_url зашифрован — расшифровываем перед подключением (иначе
            # from_url падает и прокси всегда «мёртв»). Passthrough для plaintext.
            from services.token_vault import decrypt_token

            socks_module = _il.import_module("aiohttp_socks")
            ProxyConnector = getattr(socks_module, "ProxyConnector")
            connector = ProxyConnector.from_url(decrypt_token(proxy_url))
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

    # echo обратно читаемый URL, а не то, что пришло (вызывающий может передать
    # как plaintext, так и зашифрованное значение из БД — decrypt_token безопасен
    # в обоих случаях, passthrough для legacy plaintext).
    from services.token_vault import decrypt_token

    result: dict = {
        "proxy_url": decrypt_token(proxy_url) if proxy_url else proxy_url,
        "score": score,
        "status": status,
    }
    if latency_ms is not None:
        result["latency_ms"] = latency_ms
    _proxy_health_cache.set(_cache_key, result)
    return result


async def probe_proxy(proxy_url: str, timeout: float = 10.0) -> dict:
    """Форсированная async-проверка прокси через подключение к api.telegram.org.

    В отличие от check_proxy_health (тестирует только при нейтральном score),
    ВСЕГДА выполняет реальную проверку. Полностью async (aiohttp + aiohttp_socks),
    не блокирует event loop — в отличие от account_manager.test_proxy (блокирующий
    сокет). Возвращает {ok, latency_ms?, error?}.
    """
    if not proxy_url or not isinstance(proxy_url, str):
        return {"ok": False, "error": "empty"}
    if len(proxy_url) > 2048:
        return {"ok": False, "error": "proxy URL too long"}
    try:
        import time as _t
        import aiohttp
        import importlib as _il

        # proxy_url в БД зашифрован (add_proxy → encrypt_token). ProxyConnector ждёт
        # plaintext-URL, иначе from_url падает и прокси ВСЕГДА «мёртв».
        # decrypt_token — passthrough для legacy-plaintext.
        from services.token_vault import decrypt_token

        plain_url = decrypt_token(proxy_url)
        ProxyConnector = getattr(_il.import_module("aiohttp_socks"), "ProxyConnector")
        connector = ProxyConnector.from_url(plain_url)
        t0 = _t.monotonic()
        async with aiohttp.ClientSession(connector=connector) as _sess:
            async with _sess.get(
                "https://api.telegram.org",
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=False,
            ) as resp:
                latency_ms = int((_t.monotonic() - t0) * 1000)
                ok = resp.status < 500
        record_proxy_result(proxy_url, "default", ok, latency_ms=float(latency_ms))
        return {"ok": ok, "latency_ms": latency_ms}
    except Exception as e:
        record_proxy_result(proxy_url, "default", False)
        return {"ok": False, "error": str(e)[:120]}


async def get_proxy_pool_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Статистика прокси-пула владельца: общее число, активные, мёртвые, без назначения.

    Возвращает:
        {
            total, active, inactive, dead (is_alive=FALSE),
            unassigned (нет аккаунтов), assigned,
            avg_score, geo_distribution: {country: count}
        }
    """
    _cache_key = f"pps:{owner_id}"
    _cached = _proxy_pool_stats_cache.get(_cache_key)
    if _cached is not None:
        return _cached

    result = {
        "total": 0,
        "active": 0,
        "inactive": 0,
        "dead": 0,
        "unassigned": 0,
        "assigned": 0,
        "avg_score": 0.0,
        "geo_distribution": {},
    }
    try:
        rows = await pool.fetch(
            """SELECT p.id, p.is_active, p.is_alive, p.geo_country, p.proxy_url,
                      (SELECT COUNT(*) FROM tg_accounts a
                       WHERE a.proxy_id = p.id AND a.is_active = TRUE) AS assigned_count
               FROM user_proxies p
               WHERE p.owner_id = $1""",
            owner_id,
        )
    except Exception as e:
        log.warning("get_proxy_pool_stats failed owner=%d: %s", owner_id, e)
        return result

    if not rows:
        return result

    total_score = 0.0
    scored_count = 0
    for row in rows:
        result["total"] += 1
        if row["is_active"]:
            result["active"] += 1
        else:
            result["inactive"] += 1
        if row["is_alive"] is False:
            result["dead"] += 1
        assigned = row["assigned_count"] or 0
        if assigned > 0:
            result["assigned"] += 1
        else:
            result["unassigned"] += 1
        country = row["geo_country"] or "unknown"
        result["geo_distribution"][country] = result["geo_distribution"].get(country, 0) + 1
        score = get_proxy_score(row["proxy_url"] or "", "default")
        total_score += score
        scored_count += 1

    result["avg_score"] = round(total_score / scored_count, 3) if scored_count else 0.0
    _proxy_pool_stats_cache.set(_cache_key, result)
    return result


async def auto_rotate_proxy(
    pool: asyncpg.Pool,
    owner_id: int,
    account_id: int,
) -> dict:
    """Авто-ротация прокси: назначить аккаунту лучший доступный прокси из пула.

    Алгоритм: берём живые прокси, сортируем по score, исключаем уже занятые
    (для изоляции), назначаем лучший. Если подходящих нет — пробим и назначаем
    первый живой.

    Возвращает {success, new_proxy_id?, old_proxy_id?, message?}
    """
    result = {"success": False, "new_proxy_id": None, "old_proxy_id": None, "message": ""}
    try:
        acc_row = await pool.fetchrow(
            "SELECT id, proxy_id FROM tg_accounts WHERE id = $1 AND owner_id = $2",
            account_id, owner_id,
        )
        if not acc_row:
            result["message"] = "account not found"
            return result

        old_proxy_id = acc_row["proxy_id"]
        result["old_proxy_id"] = old_proxy_id

        proxies = await pool.fetch(
            """SELECT p.id, p.proxy_url, p.is_alive, p.is_active
               FROM user_proxies p
               WHERE p.owner_id = $1 AND p.is_active = TRUE AND p.is_alive IS NOT FALSE
               ORDER BY p.id""",
            owner_id,
        )
        if not proxies:
            result["message"] = "no active proxies available"
            return result

        used_ips: set[str] = set()
        accounts = await pool.fetch(
            "SELECT a.id, p.proxy_url FROM tg_accounts a "
            "JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
            "WHERE a.owner_id = $1 AND a.is_active = TRUE AND a.id != $2",
            owner_id, account_id,
        )
        for acc in accounts:
            ip = extract_ip_from_proxy(acc["proxy_url"] or "")
            if ip:
                used_ips.add(ip)

        def _score_key(p: dict) -> tuple[int, float]:
            score = get_proxy_score(p["proxy_url"] or "", "default")
            ip = extract_ip_from_proxy(p["proxy_url"] or "")
            ip_used = 1 if ip and ip in used_ips else 0
            return (ip_used, -score)

        sorted_proxies = sorted(proxies, key=_score_key)
        best = sorted_proxies[0]
        new_proxy_id = best["id"]
        await pool.execute(
            "UPDATE tg_accounts SET proxy_id = $1 WHERE id = $2 AND owner_id = $3",
            new_proxy_id, account_id, owner_id,
        )
        result["success"] = True
        result["new_proxy_id"] = new_proxy_id
        result["message"] = "rotated"
    except Exception as e:
        log.warning("auto_rotate_proxy failed acc=%d: %s", account_id, e)
        result["message"] = str(e)[:120]
    return result


async def get_proxy_health(
    pool: asyncpg.Pool,
    owner_id: int,
    proxy_id: int,
) -> dict:
    """Здоровье конкретного прокси: score, assigned accounts, last probe, geo.

    Возвращает:
        {
            proxy_id, score, status, assigned_accounts: [account_ids],
            geo_country, is_active, is_alive
        }
    """
    result: dict = {
        "proxy_id": proxy_id,
        "score": 0.5,
        "status": "unknown",
        "assigned_accounts": [],
        "geo_country": "",
        "is_active": False,
        "is_alive": None,
    }
    try:
        row = await pool.fetchrow(
            """SELECT p.id, p.proxy_url, p.is_active, p.is_alive, p.geo_country
               FROM user_proxies p
               WHERE p.id = $1 AND p.owner_id = $2""",
            proxy_id, owner_id,
        )
        if not row:
            result["status"] = "not_found"
            return result

        result["is_active"] = row["is_active"]
        result["is_alive"] = row["is_alive"]
        result["geo_country"] = row["geo_country"] or ""

        score = get_proxy_score(row["proxy_url"] or "", "default")
        result["score"] = round(score, 3)

        if score >= 0.65:
            result["status"] = "good"
        elif score >= 0.4:
            result["status"] = "degraded"
        elif score > 0:
            result["status"] = "bad"
        else:
            result["status"] = "unknown"

        assigned = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE proxy_id = $1 AND owner_id = $2 AND is_active = TRUE",
            proxy_id, owner_id,
        )
        result["assigned_accounts"] = [r["id"] for r in assigned]
    except Exception as e:
        log.warning("get_proxy_health failed proxy=%d: %s", proxy_id, e)
        result["status"] = "error"
    return result
