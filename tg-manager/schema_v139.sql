-- v139: режим совпадения ключевого слова в авто-ответах
-- (contains — вхождение, exact — точное сообщение, starts — начинается с).
ALTER TABLE auto_replies ADD COLUMN IF NOT EXISTS match_mode TEXT DEFAULT 'contains';
