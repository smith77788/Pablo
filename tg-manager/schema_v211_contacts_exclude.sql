-- Контакты: пометка «личный/исключён из работы».
-- Проблема: личные контакты владельца смешались с общими в едином хранилище и
-- попадали в рабочие сегменты (рассылки/инвайты по контактам). Флаг excluded
-- убирает контакт из РАБОЧИХ срезов (resolve_segment/count_segment), но НЕ из
-- обычного просмотра списка — владелец их видит и может снять пометку.

ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS excluded_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_uc_excluded ON unified_contacts(owner_id) WHERE excluded;
