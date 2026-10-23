-- v136: индексы горячих путей.
-- ИСПРАВЛЕНО (две ошибки, ронявшие миграцию на каждом свежем деплое):
--  (1) частичный индекс с предикатом NOW() невозможен — функции в предикате
--      индекса должны быть IMMUTABLE, NOW() таковой не является. Заменён на
--      плоский индекс по cooldown_until (работает для запросов cooldown_until > X).
--  (2) индексы channel_members перенесены в v144 — таблица создаётся в v137,
--      т.е. ПОЗЖЕ этого файла; здесь её ещё нет → "relation does not exist".
CREATE INDEX IF NOT EXISTS idx_operation_queue_owner_status ON operation_queue(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_operation_queue_scheduled ON operation_queue(scheduled_for) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_tg_accounts_owner_active ON tg_accounts(owner_id, is_active);
CREATE INDEX IF NOT EXISTS idx_tg_accounts_cooldown_active ON tg_accounts(cooldown_until);
CREATE INDEX IF NOT EXISTS idx_warmup_sessions_status ON warmup_sessions(status) WHERE status = 'active';
