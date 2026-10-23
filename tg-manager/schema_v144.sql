-- v144: индексы channel_members (перенесены из v136).
-- Таблица channel_members создаётся в v137, поэтому индексы по ней должны идти
-- в файле с номером > 137 — иначе миграция падает с "relation does not exist".
CREATE INDEX IF NOT EXISTS idx_channel_members_user ON channel_members(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_members_channel ON channel_members(channel_id);
