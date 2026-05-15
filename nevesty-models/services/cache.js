// ─── Simple in-memory TTL cache ───────────────────────────────────────────────
// No external dependencies — pure Map with expiry timestamps.
// Used for: bot_settings (5 min TTL), catalog models (2 min TTL), counters (30s TTL).

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

// TTL constants (ms)
const TTL_SETTINGS = 5 * 60 * 1000; // 5 min — stable config values
const TTL_CATALOG = 2 * 60 * 1000; // 2 min — model catalog pages
const TTL_COUNTER = 30 * 1000; // 30 s  — frequently changing counters

const cache = new SimpleCache(TTL_SETTINGS);

module.exports = { cache, TTL_SETTINGS, TTL_CATALOG, TTL_COUNTER };
