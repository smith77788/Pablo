-- Честный per-account счётчик исходов Strike в истории атак.
-- Сводка в момент запуска уже показывает принято/флуд/бан/сбой
-- (StrikeResult.accounts_ok/flood/banned/failed), но вкладка «История» их не
-- хранила — там был виден только peer_reported. Добавляем разбивку в
-- strike_history, чтобы история отражала реальную картину нагрузки на флот.
-- Аддитивно, DEFAULT 0 — старые вставки и чтения не ломаются.
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_ok     INT DEFAULT 0;
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_flood  INT DEFAULT 0;
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_banned INT DEFAULT 0;
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS accounts_failed INT DEFAULT 0;
