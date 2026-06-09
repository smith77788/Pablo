"""
IP Lock — distributed per-account session lock.

Prevents two parallel workers from opening a Telethon session for the
same account simultaneously (session collision / auth key conflicts).

Priority backend: Redis (SET NX + TTL) — requires REDIS_URL env var.
Fallback: PostgreSQL advisory locks (deterministic bigint hash of account_id).
Last resort: in-process asyncio.Lock (single-replica safe).

Timeout: max _LOCK_WAIT seconds to acquire; raises asyncio.TimeoutError.
Auto-release: Redis TTL + context manager ensure no leaked locks on crash.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

log = logging.getLogger(__name__)

_LOCK_TTL: int = 300          # Redis lock TTL (safety cap), seconds
_LOCK_WAIT: float = 30.0      # max wait to acquire lock
_LOCK_POLL: float = 0.25      # retry interval when spinning
_REDIS_PREFIX: str = "session_lock:"

# Lazy Redis client — initialised once on first use
_redis = None
_redis_checked: bool = False
_redis_init_lock = asyncio.Lock()

# In-process fallback locks
_mem_locks: dict[int, asyncio.Lock] = {}
_mem_guard = asyncio.Lock()


# ── Redis init ────────────────────────────────────────────────────────────────

async def _get_redis():
    """Lazy-init Redis client. Returns None if REDIS_URL not set or Redis down."""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    async with _redis_init_lock:
        if _redis_checked:
            return _redis
        _redis_checked = True
        try:
            import config  # type: ignore
            url = getattr(config, "REDIS_URL", "") or ""
            if not url:
                log.debug("ip_lock: REDIS_URL not configured — using PG advisory locks")
                return None
            import redis.asyncio as aioredis  # type: ignore
            client = aioredis.from_url(url, decode_responses=True, socket_timeout=5)
            await client.ping()
            _redis = client
            log.info("ip_lock: Redis backend ready (%s)", url.split("@")[-1] if "@" in url else url)
        except ImportError:
            log.debug("ip_lock: redis package not installed — using PG advisory locks")
        except Exception as exc:
            log.warning("ip_lock: Redis unavailable (%s) — falling back to PG advisory locks", exc)
        return _redis


# ── Public API ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def session_lock(
    account_id: int,
    pool: Optional[asyncpg.Pool] = None,
    timeout: float = _LOCK_WAIT,
) -> AsyncIterator[None]:
    """
    Exclusive session lock for account_id across all workers.

    Usage:
        async with session_lock(account_id, pool):
            # open and use Telethon session safely
    """
    r = await _get_redis()
    if r is not None:
        async with _redis_lock(r, account_id, timeout):
            yield
    elif pool is not None:
        async with _pg_advisory_lock(pool, account_id, timeout):
            yield
    else:
        async with _in_memory_lock(account_id, timeout):
            yield


# ── Redis backend ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def _redis_lock(r, account_id: int, timeout: float) -> AsyncIterator[None]:
    key = f"{_REDIS_PREFIX}{account_id}"
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            ok = await r.set(key, "1", nx=True, ex=_LOCK_TTL)
            if ok:
                acquired = True
                log.debug("ip_lock: Redis ACQUIRED acc=%d", account_id)
                break
            await asyncio.sleep(_LOCK_POLL)
        if not acquired:
            raise asyncio.TimeoutError(
                f"ip_lock: Redis lock timeout for account={account_id} after {timeout:.0f}s"
            )
        yield
    finally:
        if acquired:
            try:
                await r.delete(key)
                log.debug("ip_lock: Redis RELEASED acc=%d", account_id)
            except Exception as exc:
                log.warning("ip_lock: Redis release failed acc=%d: %s", account_id, exc)


# ── PostgreSQL advisory lock backend ─────────────────────────────────────────

def _pg_key(account_id: int) -> int:
    """Map account_id → deterministic 63-bit PG advisory lock key."""
    raw = hashlib.sha256(f"ip_lock:{account_id}".encode()).digest()
    return int.from_bytes(raw[:8], "big") % (2**63 - 1)


@asynccontextmanager
async def _pg_advisory_lock(
    pool: asyncpg.Pool,
    account_id: int,
    timeout: float,
) -> AsyncIterator[None]:
    lock_key = _pg_key(account_id)
    deadline = time.monotonic() + timeout
    conn = await pool.acquire()
    acquired = False
    try:
        while time.monotonic() < deadline:
            ok = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
            if ok:
                acquired = True
                log.debug("ip_lock: PG advisory ACQUIRED acc=%d key=%d", account_id, lock_key)
                break
            await asyncio.sleep(_LOCK_POLL)
        if not acquired:
            await pool.release(conn)
            raise asyncio.TimeoutError(
                f"ip_lock: PG advisory lock timeout for account={account_id}"
            )
        yield
    finally:
        if acquired:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", lock_key)
                log.debug("ip_lock: PG advisory RELEASED acc=%d", account_id)
            except Exception as exc:
                log.warning("ip_lock: PG unlock failed acc=%d: %s", account_id, exc)
        await pool.release(conn)


# ── In-process fallback ───────────────────────────────────────────────────────

@asynccontextmanager
async def _in_memory_lock(account_id: int, timeout: float) -> AsyncIterator[None]:
    async with _mem_guard:
        if account_id not in _mem_locks:
            _mem_locks[account_id] = asyncio.Lock()
    lock = _mem_locks[account_id]
    try:
        await asyncio.wait_for(asyncio.shield(lock.acquire()), timeout=timeout)
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(
            f"ip_lock: in-memory lock timeout for account={account_id}"
        )
    log.debug("ip_lock: in-memory ACQUIRED acc=%d", account_id)
    try:
        yield
    finally:
        lock.release()
        log.debug("ip_lock: in-memory RELEASED acc=%d", account_id)
