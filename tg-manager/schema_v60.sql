-- schema_v60: lang_code per tg_account для разнообразия фингерпринтов клиентов
ALTER TABLE tg_accounts
    ADD COLUMN IF NOT EXISTS lang_code         TEXT DEFAULT 'ru',
    ADD COLUMN IF NOT EXISTS system_lang_code  TEXT DEFAULT 'ru-RU';

-- Распределить существующие аккаунты по языкам (60% ru, 40% — вариации).
-- Только аккаунты с дефолтным значением получат новое — персональные настройки не затронуты.
UPDATE tg_accounts
SET
    lang_code = (ARRAY[
        'ru','ru','ru','ru','ru','ru',
        'en','uk','de','fr','it','es','pl','tr','be'
    ])[((id - 1) % 15) + 1],
    system_lang_code = (ARRAY[
        'ru-RU','ru-RU','ru-RU','ru-RU','ru-RU','ru-RU',
        'en-US','uk-UA','de-DE','fr-FR','it-IT','es-ES','pl-PL','tr-TR','be-BY'
    ])[((id - 1) % 15) + 1]
WHERE lang_code = 'ru' AND system_lang_code = 'ru-RU';
