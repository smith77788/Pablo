-- v260: Почтовые аккаунты для отправки email
CREATE TABLE IF NOT EXISTS email_accounts (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    email           TEXT NOT NULL,
    smtp_host       TEXT NOT NULL,
    smtp_port       INTEGER NOT NULL DEFAULT 587,
    smtp_user       TEXT NOT NULL,
    smtp_pass_enc   TEXT NOT NULL,
    display_name    TEXT DEFAULT '',
    daily_limit     INTEGER NOT NULL DEFAULT 50,
    sent_today      INTEGER NOT NULL DEFAULT 0,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, email)
);

CREATE INDEX IF NOT EXISTS idx_email_accounts_owner ON email_accounts(owner_id);
