-- Индекс для уборки ленты уведомлений.
--
-- services/db_maintenance.py удаляет строки старше 60 дней по created_at. У
-- таблицы уже есть индекс (owner_id, created_at DESC), но уборка идёт по ВСЕМ
-- владельцам: ведущая колонка owner_id в условие не попадает, поэтому каждая
-- пачка удаления читала бы таблицу целиком и уборка не заканчивалась бы.
-- Нужен индекс, ведущий по дате и без WHERE (сторож —
-- tests/test_retention_tables_have_indexes.py).

CREATE INDEX IF NOT EXISTS idx_notification_feed_created
    ON notification_feed(created_at);
