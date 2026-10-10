CREATE TABLE IF NOT EXISTS user_proxies (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    label       TEXT,
    proxy_url   TEXT NOT NULL,
    proxy_type  TEXT DEFAULT 'socks5',
    is_active   BOOLEAN DEFAULT true,
    last_check  TIMESTAMPTZ,
    is_alive    BOOLEAN,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, proxy_url)
);
CREATE INDEX IF NOT EXISTS idx_proxies_owner ON user_proxies(owner_id);
