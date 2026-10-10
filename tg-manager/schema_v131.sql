-- v131: хранить инлайн-кнопки рассылки, чтобы они переживали рестарт процесса
-- (resume_interrupted докатывает рассылку и должен восстановить кнопки).
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS buttons JSONB;
