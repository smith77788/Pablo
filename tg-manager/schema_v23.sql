-- v23: unique device fingerprints per Telegram account

ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS device_model   TEXT,
    ADD COLUMN IF NOT EXISTS system_version TEXT,
    ADD COLUMN IF NOT EXISTS app_version    TEXT;

-- Distribute existing accounts across realistic devices.
-- WHERE NULL is idempotent: re-runs won't overwrite already-assigned fingerprints.
UPDATE tg_accounts
SET
    device_model = (ARRAY[
        'Samsung SM-S928B', 'Samsung SM-S918B', 'Samsung SM-S911B', 'Samsung SM-A546B',
        'Xiaomi 14 Pro',    'Xiaomi 13T Pro',   'Xiaomi Redmi Note 13 Pro',
        'Google Pixel 8 Pro', 'Google Pixel 7a',
        'OnePlus 12',  'OnePlus 11', 'POCO X6 Pro', 'realme GT 5 Pro',
        'Motorola Edge 50 Pro', 'Samsung SM-A336B', 'Xiaomi POCO M5s',
        'Samsung SM-A135F', 'Vivo V27 Pro', 'Nokia G60 5G', 'Motorola Moto G84'
    ])[((id - 1) % 20) + 1],
    system_version = (ARRAY[
        'Android 14', 'Android 14', 'Android 14', 'Android 13',
        'Android 14', 'Android 13', 'Android 13', 'Android 14',
        'Android 13', 'Android 14', 'Android 13', 'Android 14',
        'Android 14', 'Android 14', 'Android 12', 'Android 12',
        'Android 13', 'Android 13', 'Android 12', 'Android 13'
    ])[((id - 1) % 20) + 1],
    app_version = (ARRAY[
        '10.14.4', '10.14.3', '10.13.2', '10.12.2', '10.11.0',
        '10.10.1', '10.9.1',  '10.8.2',  '10.14.4', '10.13.2',
        '10.12.2', '10.11.0', '10.14.3', '10.10.1', '10.9.1',
        '10.8.2',  '10.14.4', '10.13.2', '10.12.2', '10.11.0'
    ])[((id - 1) % 20) + 1]
WHERE device_model IS NULL;
