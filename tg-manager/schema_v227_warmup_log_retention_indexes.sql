-- schema_v227: индексы под уборку журналов прогрева и активности.
--
-- В эти три таблицы пишется строка на КАЖДОЕ действие прогрева: на флоте в
-- тысячи аккаунтов это десятки тысяч строк в сутки, и до сих пор их никто не
-- чистил. Уборка удаляет пачками по ctid, и без индекса по времени каждая
-- пачка читает таблицу целиком — на разросшемся журнале проход не доезжает до
-- конца и таблица продолжает расти.
--
-- Индексы полные (без WHERE): уборка идёт по всей таблице.
-- У account_warmup_log уже есть (account_id, performed_at DESC) — он для
-- экрана аккаунта, для уборки по времени не годится: ведущая колонка не та.

CREATE INDEX IF NOT EXISTS idx_warmup_log_performed
    ON account_warmup_log (performed_at);

CREATE INDEX IF NOT EXISTS idx_warmup_session_log_performed
    ON warmup_session_log (performed_at);

CREATE INDEX IF NOT EXISTS idx_resource_activity_log_performed
    ON resource_activity_log (performed_at);
