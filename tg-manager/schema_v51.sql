-- Approval workflow для опасных операций
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS requires_approval BOOLEAN DEFAULT FALSE;
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS approved_by BIGINT;

CREATE INDEX IF NOT EXISTS idx_opq_approval ON operation_queue(requires_approval, status)
  WHERE requires_approval = TRUE;
