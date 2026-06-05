"""Free Proxy Pool — scrapes, validates, caches SOCKS5 proxies for accounts without personal proxies.

Background loop: refreshes every 6 hours.
Validation: HTTPS GET to api.telegram.org through proxy, 10s timeout.
Selection: random valid proxy from in-memory cache (fallback to DB if cache cold).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time as _time
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

# ── Sources of free SOCKS5 proxy lists ────────────────────────────────────────
_PROXY_SOURCES: list[str] = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_type=socks5&country=all&ssl=all&anonymity=all&simplified=true",
]

# Validation target — standard HTTPS endpoint (same as proxy_manager.py)
_VALIDATE_URL = "https://api.telegram.org/"
_VALIDATE_TIMEOUT = 10.0
_VALIDATE_CONCURRENCY = 40  # max simultaneous validation connections
_REFRESH_INTERVAL_H = 6
_MAX_FAIL_COUNT = 3  # remove proxy after this many consecutive failures
_MIN_POOL_SIZE = 20  # warn if valid pool drops below this

# ── In-memory cache ────────────────────────────────────────────────────────────
_valid_pool: list[str] = []  # proxy_urls ready for use
_pool_updated_at: float = 0.0  # monotonic timestamp of last refresh
_pool_lock = asyncio.Lock()


def _norm(raw: str) -> Optional[str]:
    """Normalise raw 'host:port' or 'socks5://host:port' to 'socks5://host:port'."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        raw = "socks5://" + raw
    # basic sanity: must have host:port after scheme
    after = raw.split("://", 1)[1]
    parts = after.rsplit(":", 1)
    if len(parts) != 2:
        return None
    try:
        int(parts[1])
    except ValueError:
        return None
    return raw.lower()


async def _fetch_source(session, url: str) -> list[str]:
    """Download one proxy list, return normalised URLs."""
    try:
        async with session.get(url, timeout=20, ssl=False) as resp:
            text = await resp.text()
        proxies = []
        for line in text.splitlines():
            n = _norm(line)
            if n:
                proxies.append(n)
        log.info("proxy_scraper: %s → %d proxies", url, len(proxies))
        return proxies
    except Exception as e:
        log.warning("proxy_scraper: source %s failed: %s", url, e)
        return []


async def _validate_one(
    proxy_url: str, sem: asyncio.Semaphore
) -> tuple[str, bool, Optional[int]]:
    """Returns (proxy_url, is_valid, latency_ms)."""
    async with sem:
        t0 = _time.monotonic()
        try:
            from aiohttp_socks import ProxyConnector
            import aiohttp

            connector = ProxyConnector.from_url(proxy_url, rdns=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    _VALIDATE_URL,
                    timeout=aiohttp.ClientTimeout(total=_VALIDATE_TIMEOUT),
                    ssl=False,
                ) as resp:
                    latency_ms = int((_time.monotonic() - t0) * 1000)
                    # Any HTTP response means proxy reached Telegram
                    return proxy_url, resp.status < 500, latency_ms
        except Exception:
            return proxy_url, False, None


async def scrape_and_refresh(pool: asyncpg.Pool) -> dict:
    """Full cycle: fetch → deduplicate → validate → store → update cache."""
    global _valid_pool, _pool_updated_at

    import aiohttp

    log.info("proxy_scraper: starting scrape cycle")
    t_start = _time.monotonic()

    # 1. Fetch all sources concurrently
    async with aiohttp.ClientSession() as session:
        tasks = [_fetch_source(session, url) for url in _PROXY_SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    raw: set[str] = set()
    for r in results:
        if isinstance(r, list):
            raw.update(r)
    log.info("proxy_scraper: fetched %d unique raw proxies", len(raw))

    if not raw:
        log.warning("proxy_scraper: no proxies fetched — all sources failed")
        # Record the attempt time so UI shows when last check ran
        try:
            from database import db as _db

            await _db.set_platform_setting(pool, "proxy_scraper_last_run", "0/0")
        except Exception:
            pass
        return {
            "fetched": 0,
            "valid": 0,
            "duration_s": int(_time.monotonic() - t_start),
        }

    # 2. Validate concurrently (limited semaphore)
    sem = asyncio.Semaphore(_VALIDATE_CONCURRENCY)
    validation_tasks = [_validate_one(url, sem) for url in raw]
    validation_results = await asyncio.gather(*validation_tasks)

    valid = [(url, lat) for url, ok, lat in validation_results if ok]
    valid.sort(key=lambda x: x[1] or 9999)  # sort by latency asc
    log.info("proxy_scraper: %d/%d proxies valid", len(valid), len(raw))

    # 3. Upsert into DB
    if valid:
        await pool.executemany(
            """INSERT INTO platform_proxy_pool (proxy_url, proxy_type, is_valid, latency_ms, last_check, fail_count)
               VALUES ($1, 'socks5', TRUE, $2, NOW(), 0)
               ON CONFLICT (proxy_url) DO UPDATE SET
                   is_valid=TRUE, latency_ms=$2, last_check=NOW(), fail_count=0""",
            [(url, lat) for url, lat in valid],
        )

    # 4. Mark everything NOT in valid set as invalid (only those already in DB)
    valid_urls = [url for url, _ in valid]
    if valid_urls:
        await pool.execute(
            """UPDATE platform_proxy_pool
               SET is_valid=FALSE, fail_count=fail_count+1
               WHERE proxy_url != ALL($1::text[]) AND is_valid=TRUE""",
            valid_urls,
        )

    # 5. Delete hard-dead proxies
    deleted = await pool.fetchval(
        "WITH d AS (DELETE FROM platform_proxy_pool WHERE fail_count >= $1 RETURNING 1) SELECT COUNT(*) FROM d",
        _MAX_FAIL_COUNT,
    )

    # 6. Update in-memory cache (both local and account_manager round-robin)
    async with _pool_lock:
        _valid_pool = valid_urls[:500]  # cap at 500 for memory
        _pool_updated_at = _time.monotonic()
    try:
        from services import account_manager

        account_manager.set_pool_proxy_cache(valid_urls[:500])
    except Exception as _e:
        log.debug("proxy_scraper: account_manager cache update failed: %s", _e)

    duration_s = int(_time.monotonic() - t_start)
    log.info(
        "proxy_scraper: cycle done — valid=%d deleted=%s duration=%ds",
        len(valid),
        deleted or 0,
        duration_s,
    )
    if len(valid) < _MIN_POOL_SIZE:
        log.warning(
            "proxy_scraper: pool size %d < minimum %d", len(valid), _MIN_POOL_SIZE
        )

    # Record last scrape timestamp so UI shows it even if pool is empty
    try:
        from database import db as _db
        import datetime

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        await _db.set_platform_setting(
            pool, "proxy_scraper_last_run", f"{len(valid)}/{len(raw)}@{now_str}"
        )
    except Exception:
        pass

    return {"fetched": len(raw), "valid": len(valid), "duration_s": duration_s}


async def get_pool_proxy(pool: asyncpg.Pool) -> Optional[str]:
    """Return a random valid proxy from the platform pool, or None if pool is empty."""
    async with _pool_lock:
        if _valid_pool:
            return random.choice(_valid_pool)

    # Cache cold — load from DB
    rows = await pool.fetch(
        "SELECT proxy_url FROM platform_proxy_pool WHERE is_valid=TRUE ORDER BY latency_ms ASC NULLS LAST LIMIT 200"
    )
    if not rows:
        return None
    urls = [r["proxy_url"] for r in rows]
    async with _pool_lock:
        _valid_pool[:] = urls
    return random.choice(urls) if urls else None


async def record_proxy_result(
    pool: asyncpg.Pool, proxy_url: str, success: bool
) -> None:
    """Update proxy stats after actual use in an operation."""
    try:
        if success:
            await pool.execute(
                "UPDATE platform_proxy_pool SET success_count=success_count+1, fail_count=0 WHERE proxy_url=$1",
                proxy_url,
            )
        else:
            await pool.execute(
                """UPDATE platform_proxy_pool
                   SET fail_count=fail_count+1,
                       is_valid=CASE WHEN fail_count+1 >= $2 THEN FALSE ELSE is_valid END
                   WHERE proxy_url=$1""",
                proxy_url,
                _MAX_FAIL_COUNT,
            )
            # Evict from in-memory cache if dead
            async with _pool_lock:
                if proxy_url in _valid_pool:
                    _valid_pool.remove(proxy_url)
    except Exception as e:
        log.debug("proxy_scraper: record_proxy_result failed: %s", e)


async def get_pool_stats(pool: asyncpg.Pool) -> dict:
    """Return stats for admin/UI display."""
    row = await pool.fetchrow(
        """SELECT
            COUNT(*) FILTER (WHERE is_valid=TRUE) AS valid_count,
            COUNT(*) AS total_count,
            AVG(latency_ms) FILTER (WHERE is_valid=TRUE AND latency_ms IS NOT NULL) AS avg_latency,
            MAX(last_check) AS last_check
           FROM platform_proxy_pool"""
    )
    valid = row["valid_count"] or 0 if row else 0
    total = row["total_count"] or 0 if row else 0
    avg_lat = int(row["avg_latency"]) if (row and row["avg_latency"]) else None
    last_check = row["last_check"] if row else None

    # If DB table is empty but scraper has run, show last run time from platform_settings
    if last_check is None:
        try:
            from database import db as _db
            import datetime

            val = await _db.get_platform_setting(pool, "proxy_scraper_last_run", "")
            if val and "@" in val:
                ts_str = val.split("@", 1)[1]
                last_check = datetime.datetime.fromisoformat(
                    ts_str.rstrip("Z")
                ).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass

    return {
        "valid": valid,
        "total": total,
        "avg_latency": avg_lat,
        "last_check": last_check,
        "last_run_info": row["valid_count"] if row else None,
    }


async def run_scraper_loop(pool: asyncpg.Pool) -> None:
    """Background loop: refresh proxy pool every 6 hours."""
    log.info(
        "proxy_scraper: background loop started (interval=%dh)", _REFRESH_INTERVAL_H
    )
    # Initial refresh after 30s startup delay (let other services initialize first)
    await asyncio.sleep(30)
    while True:
        try:
            await scrape_and_refresh(pool)
        except Exception as e:
            log.error("proxy_scraper: loop error: %s", e)
        await asyncio.sleep(_REFRESH_INTERVAL_H * 3600)
