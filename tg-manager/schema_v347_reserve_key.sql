-- v347: запасной ключ аккаунта — второй НАШ сеанс.
--
-- Сессия аккаунта умирает не только от угона: AUTH_KEY_DUPLICATED (один ключ
-- с двух адресов), отзыв ключа Telegram, сбой прокси посреди рукопожатия. До
-- сих пор умершая сессия значила «входите заново по коду из SMS», а номер у
-- купленного или зарегистрированного аккаунта владельцу обычно недоступен —
-- аккаунт терялся для флота.
--
-- Запасной ключ заводится без SMS: живой сеанс сам подтверждает вход нового
-- (auth.exportLoginToken → auth.acceptLoginToken, тот же путь, что вход по
-- QR в официальном приложении). Ключ отдельный, поэтому гибель основного его
-- не задевает, когда основной умер, флот поднимает аккаунт на запасном.
--
-- reserve_auth_hash — отпечаток запасного сеанса в списке авторизаций: дозор
--   не считает его чужим, а автозащита не закрывает.
-- reserve_checked_at — когда запасной ключ последний раз проверяли живым.
-- shield_configs.guard_reserve — заводить ли запасной ключ (включает владелец).
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS reserve_session_enc TEXT;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS reserve_auth_hash BIGINT;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS reserve_made_at TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS reserve_checked_at TIMESTAMPTZ;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS guard_reserve BOOLEAN NOT NULL DEFAULT FALSE;
