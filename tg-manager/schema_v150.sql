-- Backup Proxy (failover): помечает прокси как резервный. При падении основного
-- прокси аккаунта failover_dead_proxies переназначает аккаунт на здоровый
-- резервный прокси (раздел 13 паритета Telegram Expert → Backup Proxy).
ALTER TABLE user_proxies ADD COLUMN IF NOT EXISTS is_backup BOOLEAN DEFAULT FALSE;

-- Быстрый выбор резервных прокси владельца.
CREATE INDEX IF NOT EXISTS idx_user_proxies_owner_backup
    ON user_proxies (owner_id) WHERE is_backup = TRUE AND is_active = TRUE;
