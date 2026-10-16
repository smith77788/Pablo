-- schema_v166.sql — пол участника аудитории (таргетинг рассылок/инвайта по полу)
--
-- Каноничная копия инлайн-миграции (mini_app_api.INLINE_MIGRATIONS): та нужна
-- для self-heal прода (schema_v* там может не накатываться автоматически), эта —
-- чтобы схема-only стенды (invite/contacts/… e2e) тоже видели колонку.
-- services/gender_classifier.py размечает значение 'm' | 'f' | NULL.

ALTER TABLE parsed_audiences ADD COLUMN IF NOT EXISTS gender TEXT;
