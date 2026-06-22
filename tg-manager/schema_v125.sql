-- schema_v125: 7-дневный триал — anti-abuse через потерю прогресса
-- Создать новый аккаунт для обхода = потерять всё что настроил.

ALTER TABLE platform_users
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ DEFAULT now();

-- Для существующих пользователей: триал уже завершён (они не новые)
UPDATE platform_users
SET trial_started_at = registered_at - INTERVAL '8 days'
WHERE trial_started_at IS NULL OR trial_started_at = registered_at;
