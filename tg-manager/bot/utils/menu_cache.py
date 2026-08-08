"""Lightweight in-memory TTL cache for menu/stats data.

Used to avoid redundant DB queries on every button press.
Entries expire after `ttl` seconds; cache is per-process (lost on restart).
"""
from __future__ import annotations

import time
from typing import Any

_store: dict[str, tuple[float, Any]] = {}


def get(key: str, ttl: float = 30.0) -> Any:
    """Return cached value if not expired, else None."""
    entry = _store.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def set(key: str, value: Any) -> None:
    _store[key] = (time.monotonic(), value)


def invalidate(prefix: str) -> None:
    """Remove all keys that start with prefix."""
    for k in list(_store):
        if k.startswith(prefix):
            del _store[k]


def invalidate_user(user_id: int) -> None:
    invalidate(f"u:{user_id}:")
