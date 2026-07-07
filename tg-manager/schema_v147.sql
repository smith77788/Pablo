-- CRM-статус аккаунта (жизненный цикл, задаётся оператором) — аналог
-- «ПЕРЕМЕСТИТЬ В СТАТУС» Telegram Expert. Отдельно от acc_status (техническое
-- здоровье: active/warming/banned/… ставится системными проверками) и от
-- cluster (проектная группировка). stage — ручная воронка эксплуатации.
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS stage TEXT;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_stage
    ON tg_accounts(owner_id, stage) WHERE stage IS NOT NULL;
