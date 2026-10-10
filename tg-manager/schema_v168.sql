-- schema_v168.sql — «Модератор чатов» (Chatkeeper-подобный функционал)
--
-- Нашего бота добавляют админом в чат/канал, и он мгновенно чистит системные
-- уведомления (вошёл/вышел/закрепил/сменил фото и т.п.), приветствует новичков
-- и работает модератором (бан/мьют/кик/варны, антиспам ссылок/пересылок).
--
-- guard_chats     — чаты под охраной + их настройки (jsonb).
-- guard_warnings  — счётчик предупреждений на пользователя в чате (для /warn).
-- Каноничная копия инлайн-миграций (self-heal прода в mini_app_api).

CREATE TABLE IF NOT EXISTS guard_chats (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,           -- оператор, добавивший бота (кто промоутнул)
    chat_id     BIGINT NOT NULL,           -- id группы/супергруппы/канала
    title       TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    settings    JSONB NOT NULL DEFAULT '{}'::jsonb,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id)
);

CREATE INDEX IF NOT EXISTS idx_guard_chats_owner ON guard_chats(owner_id);

CREATE TABLE IF NOT EXISTS guard_warnings (
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    warns       INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);
