-- schema_v383: индексы под внешние ключи и выборки по аккаунту/контакту.
--
-- Postgres НЕ заводит индекс на ссылающейся колонке внешнего ключа сам. Без
-- него каждое удаление строки-родителя проверяет детей полным чтением
-- таблицы: удаление N контактов или аккаунтов стоит N полных проходов, то есть
-- растёт квадратично от объёма. Поиск шёл по каталогу (pg_constraint без
-- индекса, чей первый столбец — колонка ключа), а не по жалобам.
--
-- Замер на стенде (30 000 контактов, 20 000 строк CRM, 5000 аккаунтов):
--   удаление 2000 контактов — 570 мс, из них 357 мс проверка contact_crm и
--   127 мс contact_group_members (уникальный ключ CRM ведёт по owner_id, а
--   проверка ключа ищет только по contact_id и его не берёт);
--   удаление 500 аккаунтов — 187 мс, из них ~140 мс на четырёх таблицах ниже.
-- Массовое удаление контактов и уборка мёртвых аккаунтов — штатные действия
-- владельца флота, и на десятках тысяч строк они упирались в таймаут.
--
-- managed_channels.acc_id внешним ключом не является, но по нему читают
-- «каналы аккаунта» и счётчик каналов на экране очистки аккаунтов (подзапрос
-- на каждый аккаунт: 5000 аккаунтов × 5000 каналов = 390 мс на экран).
--
-- restriction_events.bot_id — частичный: у событий аккаунтов он пуст, а
-- удаление бота (ON DELETE SET NULL) читало всю таблицу событий.

CREATE INDEX IF NOT EXISTS idx_contact_crm_contact
    ON contact_crm (contact_id);
CREATE INDEX IF NOT EXISTS idx_contact_group_members_contact
    ON contact_group_members (contact_id);

CREATE INDEX IF NOT EXISTS idx_managed_channels_acc
    ON managed_channels (acc_id);
CREATE INDEX IF NOT EXISTS idx_search_snapshots_account
    ON search_snapshots (account_id);
CREATE INDEX IF NOT EXISTS idx_observation_state_account
    ON observation_state (account_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_items_account
    ON gift_transfer_items (account_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_items_inventory
    ON gift_transfer_items (inventory_id);

CREATE INDEX IF NOT EXISTS idx_restriction_events_bot
    ON restriction_events (bot_id) WHERE bot_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
    ON webhook_deliveries (event_id);
CREATE INDEX IF NOT EXISTS idx_experiment_assignments_experiment
    ON experiment_assignments (experiment_id);
CREATE INDEX IF NOT EXISTS idx_experiment_variants_experiment
    ON experiment_variants (experiment_id);
