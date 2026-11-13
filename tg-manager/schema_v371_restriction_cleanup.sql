-- Пауза по результату проверки сохраняется при повторном импорте токена.
ALTER TABLE managed_bots
    ADD COLUMN IF NOT EXISTS cleanup_paused BOOLEAN NOT NULL DEFAULT FALSE;
