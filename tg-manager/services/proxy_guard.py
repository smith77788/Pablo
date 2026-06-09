"""
Proxy Guard — kill-switch and pre-flight validation for Telethon sessions.

On Railway/datacenter environments all Telethon sessions MUST go through a proxy.
  • Pre-flight: verify proxy alive (HTTPS to api.telegram.org) before opening session.
  • Kill-switch: mark proxy dead → subsequent calls fail fast (no network I/O).
  • Dead-cache TTL: 300 s before re-attempting a dead proxy.
  • Off-Railway: warn only, never hard-block.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# Detect Railway/datacenter environment
_IS_RAILWAY: bool = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("RAILWAY_SERVICE_ID")
)

# proxy_url → (is_dead, marked_at_monotonic)
_proxy_state: dict[str, tuple[bool, float]] = {}
# How long to keep a proxy in dead state before re-checking
_DEAD_RECHECK_TTL: float = 300.0
# How often to re-confirm an alive proxy (10 min)
_ALIVE_RECHECK_TTL: float = 600.0
_PREFLIGHT_TIMEOUT: float = 8.0
_PREFLIGHT_URL: str = "https://api.telegram.org/"

# Serialize concurrent preflight calls to the same proxy URL
_preflight_locks: dict[str, asyncio.Lock] = {}
_pfl_guard = asyncio.Lock()


class ProxyKillSwitchError(RuntimeError):
    """Kill-switch triggered: proxy required on datacenter but unavailable or dead."""


# ── Internal helpers ──────────────────────────────────────────────────────────

def _mask(proxy_url: str) -> str:
    """Mask credentials in proxy URL for safe logging."""
    if not proxy_url:
        return "(none)"
    if "@" in proxy_url and "://" in proxy_url:
        scheme, rest = proxy_url.split("://", 1)
        _, host = rest.rsplit("@", 1)
        return f"{scheme}://***@{host}"
    return proxy_url


async def _get_preflight_lock(proxy_url: str) -> asyncio.Lock:
    async with _pfl_guard:
        if proxy_url not in _preflight_locks:
            _preflight_locks[proxy_url] = asyncio.Lock()
        return _preflight_locks[proxy_url]


# ── Public state API ──────────────────────────────────────────────────────────

def is_proxy_marked_dead(proxy_url: str) -> bool:
    """True if proxy is cached as dead AND TTL has not expired."""
    entry = _proxy_state.get(proxy_url)
    if not entry:
        return False
    is_dead, marked_at = entry
    return is_dead and (time.monotonic() - marked_at < _DEAD_RECHECK_TTL)


def mark_proxy_dead(proxy_url: str) -> None:
    """Immediately mark proxy as dead (triggers kill-switch on Railway)."""
    _proxy_state[proxy_url] = (True, time.monotonic())
    log.error("proxy_guard: KILL-SWITCH — proxy marked dead: %s", _mask(proxy_url))


def mark_proxy_alive(proxy_url: str) -> None:
    """Mark proxy as confirmed alive (clears dead state)."""
    _proxy_state[proxy_url] = (False, time.monotonic())


def needs_preflight(proxy_url: str) -> bool:
    """True if proxy needs a new pre-flight check (never checked or alive TTL expired)."""
    entry = _proxy_state.get(proxy_url)
    if entry is None:
        return True
    is_dead, marked_at = entry
    if is_dead:
        return (time.monotonic() - marked_at) >= _DEAD_RECHECK_TTL
    # Alive but stale
    return (time.monotonic() - marked_at) >= _ALIVE_RECHECK_TTL


# ── Pre-flight check ──────────────────────────────────────────────────────────

async def preflight_check(
    proxy_url: str,
    timeout: float = _PREFLIGHT_TIMEOUT,
) -> bool:
    """
    Verify proxy liveness: HTTPS GET to api.telegram.org through the proxy.
    Serialized per proxy_url to avoid thundering herd on startup.
    Returns True if alive, False if dead. Updates _proxy_state.
    """
    if not proxy_url:
        return False

    lock = await _get_preflight_lock(proxy_url)
    async with lock:
        # Re-check after acquiring lock — another coroutine may have checked already
        if not needs_preflight(proxy_url):
            return not is_proxy_marked_dead(proxy_url)

        t0 = time.monotonic()
        alive = False
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore

            connector = ProxyConnector.from_url(proxy_url, ssl=False)
            async with aiohttp.ClientSession(connector=connector) as sess:
                async with sess.get(
                    _PREFLIGHT_URL,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    ssl=False,
                ) as resp:
                    alive = resp.status < 500
        except Exception as exc:
            log.warning(
                "proxy_guard: pre-flight FAILED %s (%.1fs) — %s",
                _mask(proxy_url),
                time.monotonic() - t0,
                exc,
            )
            mark_proxy_dead(proxy_url)
            return False

        if alive:
            mark_proxy_alive(proxy_url)
            log.debug(
                "proxy_guard: pre-flight OK %s in %.1fs",
                _mask(proxy_url),
                time.monotonic() - t0,
            )
        else:
            mark_proxy_dead(proxy_url)
        return alive


# ── Main guard entrypoint ────────────────────────────────────────────────────

async def guard_session(
    proxy_url: Optional[str],
    account_id: Optional[int] = None,
) -> None:
    """
    Call before opening a Telethon session.

    Railway mode (kill-switch active):
      • No proxy → ProxyKillSwitchError immediately
      • Proxy dead (cached) → ProxyKillSwitchError immediately
      • Proxy stale/unknown → run pre-flight → raise on failure

    Non-Railway mode:
      • No proxy → log warning only, never raise
      • Dead/stale proxy → run pre-flight → log warning on failure
    """
    if _IS_RAILWAY:
        if not proxy_url:
            raise ProxyKillSwitchError(
                f"[kill-switch] account={account_id}: Railway datacenter detected "
                "but no proxy configured — refusing to expose datacenter IP."
            )
        if is_proxy_marked_dead(proxy_url):
            raise ProxyKillSwitchError(
                f"[kill-switch] account={account_id}: proxy {_mask(proxy_url)} "
                f"is dead (cached for {_DEAD_RECHECK_TTL:.0f}s)."
            )
        if needs_preflight(proxy_url):
            alive = await preflight_check(proxy_url)
            if not alive:
                raise ProxyKillSwitchError(
                    f"[kill-switch] account={account_id}: pre-flight failed "
                    f"for {_mask(proxy_url)}."
                )
    else:
        if not proxy_url:
            log.debug(
                "proxy_guard: account=%s has no proxy (non-Railway env)", account_id
            )
            return
        if is_proxy_marked_dead(proxy_url):
            log.warning(
                "proxy_guard: account=%s proxy %s is dead (will retry in %.0fs)",
                account_id,
                _mask(proxy_url),
                _DEAD_RECHECK_TTL,
            )
            return
        if needs_preflight(proxy_url):
            await preflight_check(proxy_url)


async def on_proxy_error(
    proxy_url: str,
    exc: Optional[Exception] = None,
    account_id: Optional[int] = None,
) -> None:
    """
    Call when a proxy error is observed mid-operation.
    Marks proxy dead. On Railway raises ProxyKillSwitchError immediately.
    """
    mark_proxy_dead(proxy_url)
    msg = (
        f"[kill-switch] account={account_id}: proxy {_mask(proxy_url)} "
        f"failed mid-operation: {exc}"
    )
    if _IS_RAILWAY:
        raise ProxyKillSwitchError(msg)
    log.warning("proxy_guard: %s (non-Railway, continuing)", msg)


# ── Bulk pre-flight on startup ────────────────────────────────────────────────

async def warmup_proxies(
    proxy_urls: list[str],
    concurrency: int = 8,
    timeout: float = _PREFLIGHT_TIMEOUT,
) -> dict[str, bool]:
    """
    Pre-flight all proxies concurrently on startup.
    Returns {proxy_url: is_alive} mapping.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, bool] = {}

    async def _check(url: str) -> None:
        async with semaphore:
            results[url] = await preflight_check(url, timeout=timeout)

    await asyncio.gather(*[_check(u) for u in proxy_urls], return_exceptions=True)

    alive = sum(v for v in results.values())
    dead = len(results) - alive
    log.info(
        "proxy_guard: warmup complete — %d/%d alive, %d dead",
        alive,
        len(results),
        dead,
    )
    return results
