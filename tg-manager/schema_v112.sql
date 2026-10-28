-- schema_v112: Compliance Engine — cryptographically signed audit trail

CREATE TABLE IF NOT EXISTS compliance_audit (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT,
    account_id  BIGINT,
    op_type     TEXT NOT NULL,
    op_id       BIGINT,
    params_hash TEXT,
    outcome     TEXT NOT NULL DEFAULT 'unknown',
    hmac_sig    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS compliance_audit_user_idx    ON compliance_audit(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS compliance_audit_op_idx      ON compliance_audit(op_id) WHERE op_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS compliance_audit_created_idx ON compliance_audit(created_at DESC);
