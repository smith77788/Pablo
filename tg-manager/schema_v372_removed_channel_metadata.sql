-- Новые отключения сохраняют метаданные канала для восстановления.
ALTER TABLE removed_channels
    ADD COLUMN IF NOT EXISTS catalog_metadata JSONB;
