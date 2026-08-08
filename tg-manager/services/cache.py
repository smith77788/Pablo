"""Caching Layer — in-memory cache with TTL for frequently accessed data."""

from __future__ import annotations

import time
import logging
from typing import Any, Callable
from functools import wraps

log = logging.getLogger(__name__)


class TTLCache:
    """In-memory cache with Time-To-Live expiration."""
    
    def __init__(self, default_ttl: float = 300.0, max_size: int = 1000):
        """Initialize cache.
        
        Args:
            default_ttl: Default TTL in seconds (5 minutes)
            max_size: Maximum number of cached items
        """
        self._cache: dict[str, dict[str, Any]] = {}
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Any | None:
        """Get value from cache. Returns None if expired or not found."""
        item = self._cache.get(key)
        if item is None:
            self._misses += 1
            return None
        
        if time.time() > item["expires_at"]:
            del self._cache[key]
            self._misses += 1
            return None
        
        self._hits += 1
        return item["value"]
    
    def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        """Set value in cache with optional TTL."""
        # Evict if at capacity
        if len(self._cache) >= self._max_size and key not in self._cache:
            self._evict_oldest()
        
        self._cache[key] = {
            "value": value,
            "expires_at": time.time() + (ttl or self._default_ttl),
            "created_at": time.time(),
        }
    
    def delete(self, key: str) -> bool:
        """Delete key from cache. Returns True if deleted."""
        if key in self._cache:
            del self._cache[key]
            return True
        return False
    
    def clear(self) -> int:
        """Clear all cached items. Returns count of cleared items."""
        count = len(self._cache)
        self._cache.clear()
        return count
    
    def stats(self) -> dict:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0,
        }
    
    def _evict_oldest(self) -> None:
        """Evict oldest item when at capacity."""
        if not self._cache:
            return
        oldest_key = min(self._cache, key=lambda k: self._cache[k]["created_at"])
        del self._cache[oldest_key]


# Global cache instances
default_cache = TTLCache(default_ttl=300.0, max_size=5000)
account_cache = TTLCache(default_ttl=60.0, max_size=1000)
proxy_cache = TTLCache(default_ttl=120.0, max_size=500)
stats_cache = TTLCache(default_ttl=60.0, max_size=200)
query_cache = TTLCache(default_ttl=30.0, max_size=2000)


def cached(ttl: float = 300.0, cache_instance: TTLCache | None = None):
    """Decorator to cache function results.
    
    Args:
        ttl: Time-to-live in seconds
        cache_instance: Cache to use (default: global cache)
    """
    cache = cache_instance or default_cache
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Build cache key from function name and arguments
            key_parts = [func.__name__] + [str(a) for a in args]
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)
            
            # Try cache first
            result = cache.get(cache_key)
            if result is not None:
                return result
            
            # Execute function and cache result
            result = await func(*args, **kwargs)
            cache.set(cache_key, result, ttl)
            return result
        
        wrapper.cache_clear = lambda: cache.clear()
        wrapper.cache_stats = lambda: cache.stats()
        return wrapper
    
    return decorator


def invalidate_pattern(pattern: str) -> int:
    """Invalidate all cache keys matching pattern.
    
    Args:
        pattern: Substring to match in cache keys
    
    Returns:
        Number of invalidated entries
    """
    count = 0
    for cache in [default_cache, account_cache, proxy_cache, stats_cache, query_cache]:
        keys_to_delete = [k for k in cache._cache if pattern in k]
        for key in keys_to_delete:
            cache.delete(key)
            count += 1
    return count


def cache_decorator(ttl: float = 300.0, cache_instance: TTLCache | None = None):
    """Decorator to cache function results (sync and async).

    Args:
        ttl: Time-to-live in seconds
        cache_instance: Cache to use (default: global cache)

    Usage:
        @cache_decorator(ttl=60)
        async def get_user(user_id): ...

        @cache_decorator(ttl=120)
        def compute(x): ...
    """
    cache = cache_instance or default_cache

    def decorator(func: Callable) -> Callable:
        import asyncio

        @wraps(func)
        def wrapper(*args, **kwargs):
            key_parts = [func.__name__] + [str(a) for a in args]
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)

            result = cache.get(cache_key)
            if result is not None:
                return result

            if asyncio.iscoroutinefunction(func):
                async def _async_wrapper():
                    result = await func(*args, **kwargs)
                    cache.set(cache_key, result, ttl)
                    return result
                return _async_wrapper()
            else:
                result = func(*args, **kwargs)
                cache.set(cache_key, result, ttl)
                return result

        wrapper.cache_clear = lambda: cache.clear()
        wrapper.cache_stats = lambda: cache.stats()
        return wrapper

    return decorator


def invalidate_cache(pattern: str) -> int:
    """Invalidate all cache keys matching pattern across all cache instances.

    Args:
        pattern: Substring to match in cache keys

    Returns:
        Number of invalidated entries
    """
    return invalidate_pattern(pattern)


def get_cache_stats() -> dict:
    """Get aggregated statistics across all cache instances.

    Returns:
        Dict with per-cache stats and aggregate totals.
    """
    caches = {
        "default": default_cache,
        "account": account_cache,
        "proxy": proxy_cache,
        "stats": stats_cache,
        "query": query_cache,
    }
    per_cache = {}
    total_hits = 0
    total_misses = 0
    total_size = 0
    for name, c in caches.items():
        s = c.stats()
        per_cache[name] = s
        total_hits += s["hits"]
        total_misses += s["misses"]
        total_size += s["size"]
    total = total_hits + total_misses
    return {
        "caches": per_cache,
        "total_hits": total_hits,
        "total_misses": total_misses,
        "total_size": total_size,
        "overall_hit_rate": round(total_hits / total * 100, 1) if total > 0 else 0,
    }
