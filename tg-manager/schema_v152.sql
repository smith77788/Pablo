-- Обогащение контактов данными из Telegram API: флаги, «был в сети» и
-- ОЦЕНОЧНАЯ дата регистрации (по user_id; точную Telegram не отдаёт).
-- Полный сырой снимок кладётся в digital_footprint (JSONB, schema_v148);
-- ниже — отдельные колонки под то, по чему хотим фильтровать/сортировать.
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS is_mutual   BOOLEAN DEFAULT FALSE;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS registered_estimate DATE;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS last_seen_type TEXT;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_uc_registered_est
    ON unified_contacts(owner_id, registered_estimate);
