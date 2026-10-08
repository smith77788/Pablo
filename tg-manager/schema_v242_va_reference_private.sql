-- Закрытые каналы-образцы: ref_username хранит «+хэш» приглашения, а адрес
-- канала после вступления — здесь, чтобы перечитывать его без повторного
-- обращения к ссылке (повторные вступления по ссылке Telegram считает флудом).
ALTER TABLE va_reference_channels ADD COLUMN IF NOT EXISTS peer_id BIGINT;
ALTER TABLE va_reference_channels ADD COLUMN IF NOT EXISTS peer_hash BIGINT;
