-- schema_v79: platform_proxy_pool — глобальный пул бесплатных прокси
-- Scraped автоматически каждые 6 часов, доступен всем аккаунтам без личного прокси.
CREATE TABLE IF NOT EXISTS platform_proxy_pool (
    id            SERIAL PRIMARY KEY,
    proxy_url     TEXT NOT NULL UNIQUE,
    proxy_type    TEXT NOT NULL DEFAULT 'socks5',
    is_valid      BOOLEAN NOT NULL DEFAULT TRUE,
    latency_ms    INT,
    fail_count    INT NOT NULL DEFAULT 0,
    success_count INT NOT NULL DEFAULT 0,
    last_check    TIMESTAMPTZ DEFAULT NOW(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_platform_proxy_valid ON platform_proxy_pool(is_valid, latency_ms ASC NULLS LAST);
