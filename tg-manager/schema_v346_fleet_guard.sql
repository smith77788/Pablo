-- v346: автозащита флота — щит сам закрывает чужие сеансы и ставит 2FA.
--
-- Дозор чужих сеансов (v340) только предупреждал: владелец узнавал об угоне,
-- но закрывать сеанс шёл сам, а угонщику хватает минут. И флот не знал,
-- на каких аккаунтах вообще нет пароля 2FA — то есть какие угоняются одним
-- кодом из SMS.
--
-- shield_configs.guard_sessions — закрывать НОВЫЕ чужие сеансы сразу, как
--   только обход их увидел (если наш сеанс старше суток: раньше Telegram не
--   даёт, FRESH_RESET_AUTHORISATION_FORBIDDEN).
-- shield_configs.guard_twofa — ставить свой случайный пароль 2FA аккаунтам
--   без пароля (сначала сохраняем, потом ставим).
-- tg_accounts.twofa_on — есть ли у аккаунта пароль 2FA (NULL — ещё не знаем).
-- tg_accounts.own_session_at — когда создан НАШ сеанс (по данным Telegram):
--   от него считаются сутки до права закрывать чужие.
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS guard_sessions BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE shield_configs ADD COLUMN IF NOT EXISTS guard_twofa BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS twofa_on BOOLEAN;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS own_session_at TIMESTAMPTZ;
