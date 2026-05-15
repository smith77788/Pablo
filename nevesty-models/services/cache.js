'use strict';

// ─── Simple in-memory TTL cache ───────────────────────────────────────────────
// No external dependencies — pure Map with expiry timestamps.
// Used for: bot_settings (5 min TTL), catalog models (2 min TTL), counters (30s TTL).
//
// When REDIS_URL is set, the module attempts to connect to Redis on first use.
// All cache reads/writes transparently use Redis when available, falling back
// to the in-memory store when Redis is unavailable or not configured.

// TTL constants (ms)
const TTL_SETTINGS = 5 * 60 * 1000; // 5 min — stable config values
const TTL_CATALOG = 2 * 60 * 1000; // 2 min — model catalog pages
const TTL_COUNTER = 30 * 1000; // 30 s  — frequently changing counters

// ─── Redis bootstrap (lazy, graceful) ─────────────────────────────────────────
const REDIS_URL = process.env.REDIS_URL || null;
let _redisClient = null; // null = not yet tried; false = unavailable
let _redisReady = false;

/**
 * Returns an ioredis client if Redis is configured and reachable, otherwise null.
 * The connection attempt is made once; subsequent calls return the cached result.
 */
async function getRedisClient() {
  if (!REDIS_URL) return null;
  if (_redisClient !== null) return _redisReady ? _redisClient : null;

  try {
    // ioredis is an optional peer dependency — not listed in package.json
    // so that projects without Redis don't need to install it.
    const Redis = require('ioredis');
    const client = new Redis(REDIS_URL, {
      lazyConnect: true,
      connectTimeout: 3000,
      maxRetriesPerRequest: 1,
      enableReadyCheck: false,
    });
    await client.ping();
    _redisClient = client;
    _redisReady = true;
    console.log('[cache] Redis connected:', REDIS_URL.replace(/:\/\/.*@/, '://***@'));

    client.on('error', err => {
      // Don't crash the process — log and fall back to in-memory
      if (_redisReady) {
        console.warn('[cache] Redis error, falling back to in-memory cache:', err.message);
        _redisReady = false;
      }
    });
    client.on('ready', () => {
      if (!_redisReady) {
        console.log('[cache] Redis reconnected.');
        _redisReady = true;
      }
    });

    return _redisClient;
  } catch (err) {
    console.warn('[cache] Redis unavailable, using in-memory cache:', err.message);
    _redisClient = false; // mark as "tried and failed"
    _redisReady = false;
    return null;
  }
}

// Kick off the connection attempt in the background (non-blocking)
if (REDIS_URL) {
  getRedisClient().catch(() => {}); // errors are handled inside
}

// ─── In-memory fallback ───────────────────────────────────────────────────────

class SimpleCache {
  constructor(ttlMs = 5 * 60 * 1000) {
    this._store = new Map();
    this._defaultTtl = ttlMs;
    this._hits = 0;
    this._misses = 0;
  }

  /** @param {string} key @param {number} [_ttlMs] @returns {*|undefined} */
  get(key, _ttlMs) {
    const entry = this._store.get(key);
    if (!entry) {
      this._misses++;
      return undefined;
    }
    if (Date.now() > entry.expiresAt) {
      this._store.delete(key);
      this._misses++;
      return undefined;
    }
    this._hits++;
    return entry.value;
  }

  /** @param {string} key @param {*} value @param {number} [ttlMs] */
  set(key, value, ttlMs) {
    const ttl = typeof ttlMs === 'number' && ttlMs > 0 ? ttlMs : this._defaultTtl;
    this._store.set(key, { value, expiresAt: Date.now() + ttl });
  }

  /** Remove a single key */
  del(key) {
    this._store.delete(key);
  }

  /** Remove all keys that match a prefix */
  delByPrefix(prefix) {
    for (const key of this._store.keys()) {
      if (key.startsWith(prefix)) this._store.delete(key);
    }
  }

  /** Clear the entire cache */
  clear() {
    this._store.clear();
  }

  /** Return diagnostic stats */
  stats() {
    // Prune expired entries before counting
    const now = Date.now();
    for (const [key, entry] of this._store.entries()) {
      if (now > entry.expiresAt) this._store.delete(key);
    }
    return {
      keys: this._store.size,
      hits: this._hits,
      misses: this._misses,
      hit_rate:
        this._hits + this._misses > 0 ? Math.round((this._hits / (this._hits + this._misses)) * 100) + '%' : 'n/a',
    };
  }
}

// ─── Unified cache facade ─────────────────────────────────────────────────────
// Exposes the same synchronous API as SimpleCache but transparently delegates
// to Redis when available. Redis operations are fire-and-forget for writes and
// async-with-fallback for reads (the synchronous get() checks in-memory first).

const _mem = new SimpleCache(TTL_SETTINGS);

const cache = {
  /**
   * Synchronous read from in-memory store.
   * Also schedules an async Redis read to warm the in-memory cache on miss.
   */
  get(key, ttlMs) {
    // Always serve from memory first (instant, synchronous)
    const memVal = _mem.get(key, ttlMs);
    if (memVal !== undefined) return memVal;

    // On miss, asynchronously fetch from Redis and warm in-memory for next call
    if (_redisReady && _redisClient) {
      _redisClient
        .get(key)
        .then(raw => {
          if (raw !== null) {
            try {
              const parsed = JSON.parse(raw);
              // Re-populate in-memory with remaining TTL (we don't know exact TTL,
              // use the provided ttlMs or the default catalog TTL)
              _mem.set(key, parsed, ttlMs || TTL_CATALOG);
            } catch (_) {}
          }
        })
        .catch(() => {});
    }

    return undefined;
  },

  /** Write to both in-memory and Redis (Redis is fire-and-forget). */
  set(key, value, ttlMs) {
    _mem.set(key, value, ttlMs);

    if (_redisReady && _redisClient) {
      const ttlSec = Math.ceil((typeof ttlMs === 'number' && ttlMs > 0 ? ttlMs : TTL_SETTINGS) / 1000);
      _redisClient
        .set(key, JSON.stringify(value), 'EX', ttlSec)
        .catch(err => console.warn('[cache] Redis set error:', err.message));
    }
  },

  /** Delete a single key from both stores. */
  del(key) {
    _mem.del(key);

    if (_redisReady && _redisClient) {
      _redisClient.del(key).catch(() => {});
    }
  },

  /**
   * Delete all keys matching a prefix from in-memory.
   * Redis SCAN-based prefix delete runs asynchronously.
   */
  delByPrefix(prefix) {
    _mem.delByPrefix(prefix);

    if (_redisReady && _redisClient) {
      // Use SCAN to find matching keys without blocking Redis
      const scanAndDelete = async (cursor = '0') => {
        try {
          const [nextCursor, keys] = await _redisClient.scan(cursor, 'MATCH', `${prefix}*`, 'COUNT', 100);
          if (keys.length) await _redisClient.del(...keys);
          if (nextCursor !== '0') await scanAndDelete(nextCursor);
        } catch (err) {
          console.warn('[cache] Redis delByPrefix error:', err.message);
        }
      };
      scanAndDelete().catch(() => {});
    }
  },

  /** Clear all in-memory entries. Flushes the entire Redis DB — use with care. */
  clear() {
    _mem.clear();

    if (_redisReady && _redisClient) {
      _redisClient.flushdb().catch(() => {});
    }
  },

  /** Diagnostic stats (in-memory only; Redis key count requires a DBSIZE call). */
  stats() {
    const s = _mem.stats();
    return {
      ...s,
      backend: _redisReady ? 'redis' : 'memory',
      redis_url: REDIS_URL ? REDIS_URL.replace(/:\/\/.*@/, '://***@') : null,
    };
  },
};

// Periodically evict expired entries from the in-memory store
setInterval(
  () => {
    const now = Date.now();
    for (const [key, entry] of _mem._store) {
      if (entry.expiresAt <= now) _mem._store.delete(key);
    }
  },
  10 * 60 * 1000
).unref(); // every 10 minutes, non-blocking

module.exports = { cache, TTL_SETTINGS, TTL_CATALOG, TTL_COUNTER };
