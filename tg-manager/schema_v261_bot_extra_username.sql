-- schema_v261: дополнительный @username бота с новым суффиксом.
--
-- С октября 2026 Telegram даёт боту второй username с окончанием
-- app / ai / agent / game / gay (Premium у владельца, один на бота, основной
-- «…bot» остаётся). Публичного Bot API для его установки нет — владелец
-- задаёт его у @BotFather, а мы храним, проверяем (резолв username → id бота
-- через аккаунт владельца) и показываем рядом с основным.
--
-- extra_username_verified: TRUE — резолв подтвердил, что username ведёт на
-- этого бота; FALSE — сохранён, но проверить не удалось (нет живого аккаунта,
-- flood wait, сеть) или ещё не проверяли.
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS extra_username TEXT;
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS extra_username_verified BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS extra_username_checked_at TIMESTAMPTZ;

-- Username в Telegram уникален без учёта регистра: два наших бота с одним и
-- тем же дополнительным username'ом — ошибка ввода, не допускаем.
CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_bots_extra_username
    ON managed_bots (lower(extra_username)) WHERE extra_username IS NOT NULL;
