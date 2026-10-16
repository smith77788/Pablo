from __future__ import annotations
import functools
import hashlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache system
# ---------------------------------------------------------------------------

class _CacheStore:
    """In-memory cache with TTL and stats tracking."""

    def __init__(self, max_size: int = 256, default_ttl: int = 300):
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self._stats = {"hits": 0, "misses": 0, "sets": 0, "invalidations": 0}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, value, ttl = self._store[key]
            if time.monotonic() - ts < ttl:
                self._store.move_to_end(key)
                self._stats["hits"] += 1
                return value
            del self._store[key]
        self._stats["misses"] += 1
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        if key in self._store:
            del self._store[key]
        elif len(self._store) >= self._max_size:
            self._store.popitem(last=False)
        self._store[key] = (time.monotonic(), value, ttl or self._default_ttl)
        self._stats["sets"] += 1

    def invalidate(self, pattern: Optional[str] = None) -> int:
        if pattern is None:
            count = len(self._store)
            self._store.clear()
            self._stats["invalidations"] += count
            return count
        count = 0
        keys_to_remove = [k for k in self._store if pattern in k]
        for k in keys_to_remove:
            del self._store[k]
            count += 1
        self._stats["invalidations"] += count
        return count

    def get_stats(self) -> dict:
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "invalidations": self._stats["invalidations"],
            "sets": self._stats["sets"],
        }

    def clear(self) -> None:
        self._store.clear()


_cache = _CacheStore()


def cache_decorator(ttl: Optional[int] = None, key_prefix: str = ""):
    """Decorator for caching async function results."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            cache_key = f"{key_prefix or func.__name__}:{_make_key(args, kwargs)}"
            cached = _cache.get(cache_key)
            if cached is not None:
                return cached
            result = await func(*args, **kwargs)
            _cache.set(cache_key, result, ttl)
            return result
        wrapper.invalidate = lambda pattern=None: _cache.invalidate(pattern or key_prefix)
        return wrapper
    return decorator


def _make_key(args: tuple, kwargs: dict) -> str:
    raw = json.dumps({"args": str(args), "kwargs": str(kwargs)}, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def invalidate_cache(pattern: Optional[str] = None) -> int:
    return _cache.invalidate(pattern)


def get_cache_stats() -> dict:
    return _cache.get_stats()


# ---------------------------------------------------------------------------
# Query stats tracking
# ---------------------------------------------------------------------------

class _QueryStats:
    """Track query execution statistics."""

    def __init__(self, max_entries: int = 1000):
        self._max_entries = max_entries
        self._queries: list[dict] = []
        self._totals = {"count": 0, "total_time_ms": 0.0}

    def record(self, query: str, duration_ms: float, row_count: int = 0) -> None:
        self._queries.append({
            "query": query[:200],
            "duration_ms": round(duration_ms, 3),
            "row_count": row_count,
        })
        if len(self._queries) > self._max_entries:
            self._queries = self._queries[-self._max_entries:]
        self._totals["count"] += 1
        self._totals["total_time_ms"] += duration_ms

    def get_stats(self) -> dict:
        if not self._queries:
            return {"total_queries": 0, "avg_time_ms": 0, "slowest": []}
        avg = self._totals["total_time_ms"] / self._totals["count"]
        slowest = sorted(self._queries, key=lambda x: x["duration_ms"], reverse=True)[:10]
        return {
            "total_queries": self._totals["count"],
            "avg_time_ms": round(avg, 3),
            "slowest": slowest,
        }

    def clear(self) -> None:
        self._queries.clear()
        self._totals = {"count": 0, "total_time_ms": 0.0}


_query_stats = _QueryStats()


def get_query_stats() -> dict:
    return _query_stats.get_stats()


def record_query(query: str, duration_ms: float, row_count: int = 0) -> None:
    _query_stats.record(query, duration_ms, row_count)


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------

async def batch_insert(pool, table: str, columns: list[str], rows: list[dict],
                       batch_size: int = 100) -> int:
    """Insert multiple rows in batches using executemany."""
    total_inserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        values = [tuple(row[c] for c in columns) for row in batch]
        placeholders = ", ".join(f"${j+1}" for j in range(len(columns)))
        query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        async with pool.acquire() as conn:
            await conn.executemany(query, values)
        total_inserted += len(batch)
    return total_inserted


async def batch_update(pool, table: str, updates: dict, condition_col: str,
                       condition_vals: list, batch_size: int = 100) -> int:
    """Update multiple rows in batches."""
    total_updated = 0
    set_clause = ", ".join(f"{k} = ${j+1}" for j, k in enumerate(updates.keys()))
    set_values = list(updates.values())

    for i in range(0, len(condition_vals), batch_size):
        batch = condition_vals[i:i + batch_size]
        placeholders = ", ".join(f"${len(set_values) + j + 1}" for j in range(len(batch)))
        query = f"UPDATE {table} SET {set_clause} WHERE {condition_col} IN ({placeholders})"
        result = await pool.execute(query, *set_values, *batch)
        if result.startswith("UPDATE"):
            total_updated += int(result.split()[-1])
    return total_updated
