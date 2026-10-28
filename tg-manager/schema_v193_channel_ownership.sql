-- v193: различаем «мою инфраструктуру» (владелец/админ канала) и «чужие»
-- каналы, где аккаунт лишь подписчик.
--
-- Причина: массовый импорт (_exec_channel_import_all / _exec_group_import_all)
-- тянул ВСЕ каналы-диалоги аккаунта в managed_channels, включая те, где
-- аккаунт просто участник. Из-за этого «Мои каналы» распухали чужими
-- подписками (164 против реальной инфраструктуры).
--
-- NULL = роль ещё не определена (строки, импортированные до этой миграции).
-- TRUE/FALSE проставляются при импорте по правам аккаунта в канале.
ALTER TABLE managed_channels
    ADD COLUMN IF NOT EXISTS is_admin   BOOLEAN,
    ADD COLUMN IF NOT EXISTS is_creator BOOLEAN;

-- Быстрый срез «моей инфраструктуры» по владельцу.
CREATE INDEX IF NOT EXISTS idx_managed_channels_owner_admin
    ON managed_channels(owner_id, is_admin);
