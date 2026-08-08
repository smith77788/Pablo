-- schema_v65: Infrastructure Memory tables
-- Персистентное хранилище для services/infra_memory.py

CREATE TABLE IF NOT EXISTS infra_memory_accounts (
    account_id      BIGINT       NOT NULL,
    action_type     TEXT         NOT NULL,
    successes       INTEGER      NOT NULL DEFAULT 0,
    failures        INTEGER      NOT NULL DEFAULT 0,
    last_success_at TIMESTAMPTZ,
    last_failure_at TIMESTAMPTZ,
    last_errors     TEXT[]       NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, action_type)
);

CREATE INDEX IF NOT EXISTS idx_infra_memory_accounts_account
    ON infra_memory_accounts (account_id);

CREATE TABLE IF NOT EXISTS infra_memory_proxies (
    proxy_url       TEXT         NOT NULL,
    action_type     TEXT         NOT NULL,
    successes       INTEGER      NOT NULL DEFAULT 0,
    failures        INTEGER      NOT NULL DEFAULT 0,
    avg_latency_ms  FLOAT        NOT NULL DEFAULT 0,
    last_success_at TIMESTAMPTZ,
    last_failure_at TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (proxy_url, action_type)
);
