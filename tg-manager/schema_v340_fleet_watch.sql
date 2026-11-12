-- schema_v340: дозор флота — охват проверок и чужие сеансы.
--
-- last_spambot_check_at — своя отметка проверки через @SpamBot. До неё
-- проверка ограничений и пинг живости сессии делили last_real_check_at: пинг
-- (каждые 3 часа) обновлял отметку, и выборка спам-проверки («старше 6 часов»)
-- аккаунт уже не видела — чем лучше работал пинг, тем реже проверялся спам-блок.
--
-- foreign_sessions / foreign_session_hashes / sessions_checked_at — чужие
-- авторизации аккаунта (account.getAuthorizations): любой сеанс, кроме нашего.
-- Продавец купленного аккаунта, угонщик, забытый старый вход — все они могут
-- выкинуть наш сеанс или сменить пароль. hashes — уже показанные владельцу
-- сеансы: тревога только на НОВЫЙ, а не на каждый пинг.
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS last_spambot_check_at TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS foreign_sessions INT NOT NULL DEFAULT 0;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS foreign_session_hashes BIGINT[];
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS sessions_checked_at TIMESTAMPTZ;
