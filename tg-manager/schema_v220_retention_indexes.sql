-- schema_v220: индексы под уборку журналов (services/db_maintenance.py).
--
-- Зачем. Каждые 6 часов уборка удаляет из журнальных таблиц строки старше
-- срока хранения: WHERE <дата> < NOW() - INTERVAL '...'. У восьми таблиц из
-- списка ведущей колонкой всех существующих индексов был owner_id (или
-- proxy_id, keyword_id) — по дате искать было нечем, и каждая уборка читала
-- таблицу целиком. На пустой базе это незаметно, на миллионе строк — минуты
-- чтения диска на таблицу и растущий риск, что проход не завершится вовсе.
--
-- Отдельно это стало критичным после перехода уборки на удаление пачками:
-- пачка выбирает строки тем же условием по дате, и без индекса каждая из
-- сотен пачек заново сканировала бы всю таблицу — то есть O(n^2).
--
-- Что делает. Добавляет по одному индексу на колонку даты там, где его не
-- было. Существующие индексы не трогаются: они обслуживают выборки по
-- владельцу («покажи мои события»), этот — уборку и запросы «что было
-- недавно» без фильтра по владельцу.
--
-- Почему не частичный индекс. Граница срока хранения движется во времени
-- (NOW() - INTERVAL), в предикате частичного индекса такого не записать:
-- WHERE в CREATE INDEX обязан быть IMMUTABLE.

CREATE INDEX IF NOT EXISTS idx_restriction_events_created
    ON restriction_events(created_at);

CREATE INDEX IF NOT EXISTS idx_search_rankings_checked
    ON search_rankings(checked_at);

CREATE INDEX IF NOT EXISTS idx_recovery_events_created
    ON recovery_events(created_at);

CREATE INDEX IF NOT EXISTS idx_anomaly_events_detected
    ON anomaly_events(detected_at);

CREATE INDEX IF NOT EXISTS idx_health_snapshots_at
    ON system_health_snapshots(snapshot_at);

CREATE INDEX IF NOT EXISTS idx_infra_alerts_first_seen
    ON infrastructure_alerts(first_seen_at);

CREATE INDEX IF NOT EXISTS idx_proxy_health_checked
    ON proxy_health_log(checked_at);

CREATE INDEX IF NOT EXISTS idx_operation_audit_occurred
    ON operation_audit(occurred_at);

-- Память об аккаунтах и прокси чистится по updated_at тем же проходом.
CREATE INDEX IF NOT EXISTS idx_infra_memory_accounts_updated
    ON infra_memory_accounts(updated_at);

CREATE INDEX IF NOT EXISTS idx_infra_memory_proxies_updated
    ON infra_memory_proxies(updated_at);
