-- v138: тихая рассылка (disable_notification) — сохраняем флаг, чтобы он пережил
-- рестарт процесса (resume_interrupted докатывает рассылку).
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS silent BOOLEAN NOT NULL DEFAULT FALSE;
