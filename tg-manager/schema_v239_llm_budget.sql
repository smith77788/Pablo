-- Экономия лимитов ИИ (services/llm_gate.py, channel_admin.prewrite).
--
-- Посты виртуального администратора пишутся заранее, пачками: тело кладётся в
-- строку контент-плана, и в момент публикации ИИ уже не нужен.
ALTER TABLE va_admin_plan ADD COLUMN IF NOT EXISTS body TEXT;
ALTER TABLE va_admin_plan ADD COLUMN IF NOT EXISTS written_at TIMESTAMPTZ;
ALTER TABLE va_admin_plan ADD COLUMN IF NOT EXISTS write_attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_va_admin_plan_unwritten
    ON va_admin_plan(slot_at) WHERE status = 'planned' AND body IS NULL;

-- Паузы моделей и провайдеров по лимитам: общие для всех процессов и
-- переживают рестарт. Ключ — «провайдер/модель», «провайдер» или «провайдер:free».
CREATE TABLE IF NOT EXISTS llm_cooldowns (
    key TEXT PRIMARY KEY,
    until TIMESTAMPTZ NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_cooldowns_until ON llm_cooldowns(until);

-- Учёт запросов к ИИ по провайдерам и дням.
CREATE TABLE IF NOT EXISTS llm_usage (
    day DATE NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    limited INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_day ON llm_usage(day);
