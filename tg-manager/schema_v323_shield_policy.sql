-- schema_v323: «Защита аккаунтов» — пауза, которая заканчивается, и политика щита.
--
-- Пауза щита снимала is_active и обещала «24 ч», но вернуть аккаунт в работу
-- было некому: is_active=TRUE ставит только ручное «оживить всё». Теперь
-- пауза щита помечается сроком shield_paused_until, и цикл щита сам
-- возвращает аккаунт, когда срок прошёл (если аккаунт за это время не умер и
-- владелец не выключил его сам — ручное переключение метку снимает).
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS shield_paused_until TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tg_accounts_shield_paused
    ON tg_accounts(shield_paused_until) WHERE shield_paused_until IS NOT NULL;

-- Политика щита: профиль, порог паузы (был зашит 0.9/0.8), длительность
-- охлаждения (была зашита 2 ч), реакция (только предупреждать / охлаждать /
-- охлаждать и ставить на паузу) и аккаунты, которых щит не трогает.
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS preset TEXT;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS pause_risk_threshold FLOAT;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS pause_ban_threshold FLOAT;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS cool_hours INT;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS response_mode TEXT;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS exempt_account_ids BIGINT[];

-- Из чего сложился риск: список факторов с вкладом, чтобы экран мог
-- объяснить число, а не просто его показать.
ALTER TABLE account_risk_scores ADD COLUMN IF NOT EXISTS factors JSONB;
