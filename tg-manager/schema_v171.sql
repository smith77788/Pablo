-- Vault watchdog: отметка о времени, когда владельцу УЖЕ отправили уведомление
-- о «зависании» хранилища. Не спамим одним и тем же — шлём один раз, пока не
-- придут свежие сообщения (тогда сбрасываем в NULL при архивации).
ALTER TABLE business_connections
    ADD COLUMN IF NOT EXISTS stale_notified_at TIMESTAMPTZ;
