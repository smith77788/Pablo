-- v127: добавить недостающую колонку consecutive_failures в user_proxies.
--
-- bot/handlers/proxy_manager.py трекает число подряд идущих провалов проверки
-- прокси и авто-деактивирует прокси после 3 провалов. Колонка была заявлена
-- в коде ("column added in schema migration"), но миграция не создавалась —
-- основной UPDATE всегда падал и уходил в fallback, поэтому авто-деактивация
-- не работала. DEFAULT 0 безопасен для существующих строк.

ALTER TABLE user_proxies
    ADD COLUMN IF NOT EXISTS consecutive_failures INT DEFAULT 0;
