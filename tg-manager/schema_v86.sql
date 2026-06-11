-- schema_v86: Persistent in_operation flag for tg_accounts
-- Позволяет пережить рестарт бота без race conditions между warmup/strike и op_worker.
-- Сбрасывается при старте через reset_stale_in_operation() в op_worker.py.

ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS in_operation BOOLEAN DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_in_operation
    ON tg_accounts(in_operation)
    WHERE in_operation = TRUE;
