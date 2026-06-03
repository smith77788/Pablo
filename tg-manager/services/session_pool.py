"""
Session Pool / Orchestrator — centralized Telethon session lifecycle management.

Provides:
- Session warming (connect, get_me, disconnect = auth validation)
- Session health monitoring
- Reconnect handling with DC awareness
- Account-to-session mapping with in-memory cache
- Worker-safe session checkout/checkin
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


class SessionState(str, Enum):
    UNKNOWN = "unknown"
    WARMING = "warming"
    READY = "ready"
    BUSY = "busy"
    COOLING = "cooling"
    INVALID = "invalid"
    EXPIRED = "expired"
    BANNED = "banned"


@dataclass
class SessionEntry:
    account_id: int
    session_str: str
    owner_id: int
    state: SessionState = SessionState.UNKNOWN
    last_checked: float = 0.0
    last_used: float = 0.0
    dc_id: Optional[int] = None
    user_id: Optional[int] = None
    error_count: int = 0
    device: Optional[dict] = None


# Global registry: account_id → SessionEntry
_registry: dict[int, SessionEntry] = {}
_check_lock: dict[int, asyncio.Lock] = {}


def _get_lock(account_id: int) -> asyncio.Lock:
    if account_id not in _check_lock:
        _check_lock[account_id] = asyncio.Lock()
    return _check_lock[account_id]


def register_session(
    account_id: int,
    session_str: str,
    owner_id: int,
    device: dict | None = None,
) -> SessionEntry:
    """Register or update a session in the pool."""
    if account_id in _registry:
        entry = _registry[account_id]
        entry.session_str = session_str
        entry.device = device
    else:
        entry = SessionEntry(
            account_id=account_id,
            session_str=session_str,
            owner_id=owner_id,
            device=device,
        )
        _registry[account_id] = entry
    return entry


def get_session(account_id: int) -> Optional[SessionEntry]:
    return _registry.get(account_id)


def get_state(account_id: int) -> SessionState:
    entry = _registry.get(account_id)
    return entry.state if entry else SessionState.UNKNOWN


async def warm_session(account_id: int, pool: asyncpg.Pool) -> SessionState:
    """Connect to Telegram, verify auth, get DC info. Updates state in registry."""
    from services import account_manager

    entry = _registry.get(account_id)
    if not entry:
        return SessionState.UNKNOWN

    async with _get_lock(account_id):
        if (
            entry.state == SessionState.READY
            and (time.monotonic() - entry.last_checked) < 300
        ):
            return SessionState.READY  # Fresh enough

        entry.state = SessionState.WARMING
        try:
            result = await account_manager.check_account_status_full(
                entry.session_str,
                _acc=entry.device,
                check_spambot=False,  # Quick warm: just auth check
            )
            status = result.get("status", "unknown")
            if status == "active":
                entry.state = SessionState.READY
                entry.error_count = 0
            elif status in ("banned", "deactivated"):
                entry.state = SessionState.BANNED
                await pool.execute(
                    "UPDATE tg_accounts SET is_active=FALSE, acc_status=$1, status_reason=$2 WHERE id=$3",
                    status,
                    result.get("reason", ""),
                    account_id,
                )
            elif status == "session_expired":
                entry.state = SessionState.EXPIRED
                await pool.execute(
                    "UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired' WHERE id=$1",
                    account_id,
                )
            elif status == "cooldown":
                entry.state = SessionState.COOLING
            else:
                entry.state = SessionState.INVALID
        except Exception as e:
            entry.error_count += 1
            entry.state = SessionState.INVALID
            log.warning("session_pool: warm failed acc=%d: %s", account_id, e)

        entry.last_checked = time.monotonic()
        return entry.state


async def load_from_db(pool: asyncpg.Pool, owner_id: int) -> int:
    """Load all active accounts for owner into the session pool."""
    rows = await pool.fetch(
        """SELECT a.id, a.session_str, a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.owner_id = $1 AND a.is_active = TRUE AND a.session_str IS NOT NULL""",
        owner_id,
    )
    loaded = 0
    for row in rows:
        device = (
            {
                "device_model": row["device_model"],
                "system_version": row["system_version"],
                "app_version": row["app_version"],
                "proxy_url": row["proxy_url"],
            }
            if row["device_model"]
            else None
        )
        register_session(row["id"], row["session_str"], owner_id, device)
        loaded += 1
    log.info("session_pool: loaded %d sessions for owner=%d", loaded, owner_id)
    return loaded


async def bulk_warm(pool: asyncpg.Pool, owner_id: int, concurrency: int = 3) -> dict:
    """Warm up all registered sessions for owner with limited concurrency."""
    entries = [e for e in _registry.values() if e.owner_id == owner_id]
    if not entries:
        await load_from_db(pool, owner_id)
        entries = [e for e in _registry.values() if e.owner_id == owner_id]

    semaphore = asyncio.Semaphore(concurrency)
    results: dict[int, SessionState] = {}

    async def _warm_one(entry: SessionEntry) -> None:
        async with semaphore:
            state = await warm_session(entry.account_id, pool)
            results[entry.account_id] = state
            await asyncio.sleep(0.5)  # Spread connections

    await asyncio.gather(*[_warm_one(e) for e in entries], return_exceptions=True)

    summary = {s.value: 0 for s in SessionState}
    for s in results.values():
        summary[s.value] = summary.get(s.value, 0) + 1

    log.info("session_pool: warm complete owner=%d results=%s", owner_id, summary)
    return {"total": len(results), "states": summary, "details": results}


def get_ready_count(owner_id: int) -> int:
    return sum(
        1
        for e in _registry.values()
        if e.owner_id == owner_id and e.state == SessionState.READY
    )


def get_pool_summary(owner_id: int) -> dict:
    entries = [e for e in _registry.values() if e.owner_id == owner_id]
    summary: dict[str, int] = {s.value: 0 for s in SessionState}
    for e in entries:
        summary[e.state.value] = summary.get(e.state.value, 0) + 1
    return {"total": len(entries), "states": summary}
