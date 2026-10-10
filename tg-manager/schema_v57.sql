-- Schema v57: strike_reports — история mini-strike репортов

CREATE TABLE IF NOT EXISTS strike_reports (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    target          TEXT NOT NULL,
    category        TEXT NOT NULL,
    tg_reports_sent INT  NOT NULL DEFAULT 0,
    emails_sent     INT  NOT NULL DEFAULT 0,
    total_reports   INT  NOT NULL DEFAULT 0,
    details         JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_strike_reports_owner
    ON strike_reports(owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_strike_reports_target
    ON strike_reports(target, created_at DESC);
