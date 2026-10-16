-- v140: инлайн-кнопки в авто-ответах (Bot API поддерживает reply_markup).
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS buttons JSONB;
