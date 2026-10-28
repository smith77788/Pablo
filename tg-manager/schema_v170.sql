-- schema_v170.sql — происхождение подключённого бота (Manager Mode)
--
-- created_via = 'manual'  — токен вставили руками (BotFather → скопировал);
--               'managed' — бот создан через Manager Mode (Telegram Managed Bots),
--                           токен получен автоматически getManagedBotToken.
-- Нужно для аналитики и понимания, какими ботами мы владеем через Managed API
-- (их токен можно заменить/переполучить программно).

ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS created_via TEXT DEFAULT 'manual';
