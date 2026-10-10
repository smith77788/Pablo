-- v213: массовое включение режима bot-to-bot у управляемых ботов.
--
-- Bot Mesh (координация задач между ботами) требует, чтобы у ботов-участников
-- был включён Bot-to-Bot Communication Mode. Включается он ТОЛЬКО в @BotFather
-- аккаунтом-владельцем бота (managed_bots.acc_id). Здесь — отметка состояния,
-- чтобы не гонять BotFather повторно и видеть, кто готов к сети.
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS b2b_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS b2b_checked_at TIMESTAMPTZ;
