-- schema_v312: уровни уведомлений и тумблеры для фоновых сервисов.
--
-- Уведомления фоновых циклов (лиды из сценариев и намерений, защита
-- аккаунтов, волна банов, ежедневный отчёт администратора каналов, советы
-- «организма», зависшее подключение хранилища) шли прямым send_message мимо
-- настроек и тихих часов. Теперь они идут через общий слой
-- (database.db.notify_if_enabled) с уровнем: «нужно действие» — сразу,
-- «инфо» — сразу или в сводку, «тишина» — тип выключен.
ALTER TABLE notification_settings
    ADD COLUMN IF NOT EXISTS leads        BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS reports      BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS advice       BOOLEAN NOT NULL DEFAULT TRUE,
    -- «Несрочное — только сводкой»: информационное копится весь день и
    -- приходит одним сообщением в digest_hour по местному времени.
    ADD COLUMN IF NOT EXISTS digest_only  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS digest_hour  SMALLINT NOT NULL DEFAULT 9
        CHECK (digest_hour BETWEEN 0 AND 23),
    ADD COLUMN IF NOT EXISTS last_digest_at TIMESTAMPTZ;
