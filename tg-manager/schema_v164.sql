-- schema_v164: «Хранилище» — настройки уведомлений «ловца» удалённых/изменённых.
-- По умолчанию ВКЛ: главная ценность фичи — ловить, когда собеседник удаляет или
-- редактирует сообщение. Пользователь может выключить в мини-аппе.
ALTER TABLE business_connections ADD COLUMN IF NOT EXISTS notify_deleted BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE business_connections ADD COLUMN IF NOT EXISTS notify_edited  BOOLEAN NOT NULL DEFAULT TRUE;

-- Ускоряет ленту «Недавно удалённое/изменённое» (частый экран «ловца»).
CREATE INDEX IF NOT EXISTS idx_vault_deleted ON vault_messages(owner_id, deleted_at DESC) WHERE is_deleted;
CREATE INDEX IF NOT EXISTS idx_vault_edited  ON vault_messages(owner_id, edited_at DESC)  WHERE is_edited;
