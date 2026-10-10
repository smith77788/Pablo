-- schema_v104.sql: Upsell tracking + BotMother channel settings

-- last_upsell_at: когда последний раз отправляли drip-уведомление о подписке
ALTER TABLE platform_users ADD COLUMN IF NOT EXISTS last_upsell_at TIMESTAMPTZ;

-- botmother_channel_id хранится в platform_settings (key = 'botmother_channel_id')
-- Индекс уже есть на platform_settings(key) PRIMARY KEY
