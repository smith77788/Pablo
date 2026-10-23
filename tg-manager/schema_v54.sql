-- schema_v54: Strike history persistence
CREATE TABLE IF NOT EXISTS strike_history (
    id                 SERIAL PRIMARY KEY,
    owner_id           BIGINT NOT NULL,
    target             TEXT NOT NULL,
    reason             TEXT NOT NULL DEFAULT 'spam',
    preset             TEXT,
    accounts_used      INT DEFAULT 0,
    peer_reported      INT DEFAULT 0,
    msgs_reported      INT DEFAULT 0,
    pinned_reported    INT DEFAULT 0,
    admins_reported    INT DEFAULT 0,
    network_nodes      INT DEFAULT 0,
    network_reports    INT DEFAULT 0,
    blocked            INT DEFAULT 0,
    verified_down      BOOLEAN,
    duration_s         FLOAT DEFAULT 0,
    abuse_form_ok      BOOLEAN DEFAULT FALSE,
    spambot_escalation TEXT DEFAULT 'skipped',
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_strike_history_owner ON strike_history(owner_id, created_at DESC);
