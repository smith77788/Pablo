-- v129: индексы производительности по результатам аудита.
--
-- Эти колонки используются в горячих путях (списки ботов, дашборд, лента
-- новых подписчиков, аналитика flood), но индексов на них не было —
-- запросы шли через sequential scan по растущим таблицам.

-- managed_bots.added_by — фильтр владельца в ~66 запросах (списки, дашборд,
-- воронки, лента новых пользователей). Раньше индекса не было
-- (idx_managed_bots_owner ссылался на несуществующую колонку owner_id).
CREATE INDEX IF NOT EXISTS idx_managed_bots_added_by
    ON managed_bots(added_by);

-- bot_users — лента «новых подписчиков» сортирует по first_seen по всем ботам
-- владельца. Это самая быстрорастущая таблица; сортировка шла в памяти.
CREATE INDEX IF NOT EXISTS idx_bot_users_bot_first_seen
    ON bot_users(bot_id, first_seen DESC);

-- account_flood_log — JOIN по account_id в аналитике flood (24ч). Лог-таблица
-- растёт на каждый флуд; индекса на account_id не было.
CREATE INDEX IF NOT EXISTS idx_flood_log_account
    ON account_flood_log(account_id, created_at DESC);
