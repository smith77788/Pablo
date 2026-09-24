-- Удаление точных дублей индексов.
--
-- Один и тот же индекс заводился повторно под новым именем: правки схемы шли
-- разными файлами, автор очередного не находил старый и создавал свой. В базе
-- осталось 10 групп полностью одинаковых индексов (та же таблица, те же
-- колонки в том же порядке, то же условие) плюс дубль уникального индекса
-- managed_bots.
--
-- Дубль — не безобидная копия. Каждый лишний индекс:
--   * замедляет КАЖДУЮ вставку и обновление строки в таблице (пишется он тоже);
--   * занимает место и греет кеш страницами, которые никто не читает;
--   * удлиняет VACUUM и REINDEX.
-- Дороже всего это на operation_queue и operation_log — таблицах, куда пишет
-- каждая массовая операция продукта.
--
-- Из каждой группы остаётся индекс, заведённый РАНЬШЕ (его имя уже встречается
-- в истории схемы), удаляются более поздние копии. У managed_bots остаётся
-- managed_bots_bot_id_key: за ним стоит ограничение UNIQUE и полтора десятка
-- внешних ключей, его удалить нельзя.
--
-- Данные не затрагиваются: DROP INDEX убирает только служебную структуру,
-- одинаковый близнец которой остаётся на месте. План любого запроса, который
-- пользовался удалённым индексом, переключается на оставшийся.

-- account_flood_log (account_id, created_at DESC)
DROP INDEX IF EXISTS idx_flood_log_account_created;

-- managed_bots (bot_id) UNIQUE — остаётся managed_bots_bot_id_key
DROP INDEX IF EXISTS managed_bots_bot_id_unique;

-- operation_audit (owner_id, occurred_at DESC) — их было три
DROP INDEX IF EXISTS idx_op_audit_owner_date;
DROP INDEX IF EXISTS idx_operation_audit_owner;

-- operation_log (op_id)
DROP INDEX IF EXISTS idx_operation_log_op;

-- operation_queue (owner_id, status)
DROP INDEX IF EXISTS idx_operation_queue_owner_status;

-- operation_queue (owner_id, status, created_at DESC)
DROP INDEX IF EXISTS idx_op_queue_owner_status_created;

-- platform_referrals (referred_id)
DROP INDEX IF EXISTS idx_platform_referrals_referred;

-- restriction_events (owner_id, created_at DESC)
DROP INDEX IF EXISTS idx_restr_events_owner_time;

-- subscriptions (user_id, is_active, expires_at) WHERE is_active
DROP INDEX IF EXISTS idx_subscriptions_active_user;

-- tg_accounts (owner_id, cooldown_until) WHERE cooldown_until IS NOT NULL
DROP INDEX IF EXISTS idx_tg_acc_cooldown;

-- tg_accounts (owner_id, is_active, trust_score DESC NULLS LAST)
DROP INDEX IF EXISTS idx_tg_accounts_owner_active_trust;
