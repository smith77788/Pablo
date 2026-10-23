-- Virtual Channel Administrator — уборка и чтение истории постов (va_channel_posts).
--
-- ЗАЧЕМ. В va_channel_posts пишется тело КАЖДОГО опубликованного поста на КАЖДЫЙ
-- канал: массовая публикация в 300 каналов — 300 строк до 8 КБ. Уборки у таблицы
-- не было, она росла бессрочно. Антиповтору нужны десятки последних постов, поэтому
-- db_maintenance чистит строки старше 90 дней — ему нужен индекс по дате, иначе
-- каждая пачка удаления читает таблицу целиком.
--
-- Второй индекс — для совета редактора перед массовой публикацией: она идёт во
-- все каналы сразу, и история берётся по владельцу без channel_key. Индекс
-- (owner_id, channel_key, published_at) такому запросу не помогает: Postgres
-- сортировал бы все посты владельца, чтобы взять 20 свежих.
-- Только ADD/CREATE, идемпотентно.

CREATE INDEX IF NOT EXISTS idx_va_channel_posts_published_at
    ON va_channel_posts(published_at);

CREATE INDEX IF NOT EXISTS idx_va_channel_posts_owner_recent
    ON va_channel_posts(owner_id, published_at DESC);
