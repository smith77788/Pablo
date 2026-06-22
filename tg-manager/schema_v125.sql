-- schema_v125: phone gate — один verified_phone на один платформ-пользователь

ALTER TABLE platform_users
    ADD COLUMN IF NOT EXISTS verified_phone TEXT;

-- Уникальность: один номер = один владелец (NULL допускается до верификации)
CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_users_verified_phone
    ON platform_users(verified_phone)
    WHERE verified_phone IS NOT NULL;
