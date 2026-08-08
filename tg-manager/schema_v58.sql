-- Schema v58: email-аккаунты для Strike-репортов

CREATE TABLE IF NOT EXISTS strike_email_accounts (
    id           SERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    email        TEXT NOT NULL,
    smtp_host    TEXT NOT NULL,
    smtp_port    INT  NOT NULL DEFAULT 587,
    smtp_pass    TEXT NOT NULL,
    display_name TEXT,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    fail_count   INT NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_id, email)
);

CREATE INDEX IF NOT EXISTS idx_strike_email_owner
    ON strike_email_accounts(owner_id, is_active);
