-- schema_v66: добавить avg_duration_s в infra_memory_accounts
-- Позволяет Intelligence Engine учиться на реальных временах выполнения

ALTER TABLE infra_memory_accounts
    ADD COLUMN IF NOT EXISTS avg_duration_s FLOAT NOT NULL DEFAULT 0;
