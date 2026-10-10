-- schema_v165.sql — Перехватчик ключей из внешних чатов (лидогенерация)
--
-- Аккаунт-читатель, состоящий в целевом чате/канале, периодически поллит новые
-- сообщения по ключевым словам; совпадения (лиды) пишутся в keyword_hits и
-- пересылаются оператору. Read-only поллинг (низкий риск), курсор last_msg_id
-- исключает повторную обработку, UNIQUE(watcher_id,message_id) — идемпотентность.

CREATE TABLE IF NOT EXISTS keyword_watchers (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    account_id      BIGINT NOT NULL,               -- аккаунт-читатель (в чате)
    chat_ref        TEXT   NOT NULL,               -- @username / ссылка / id чата
    chat_title      TEXT,
    keywords        JSONB  NOT NULL DEFAULT '[]',  -- список ключей (в нижнем регистре)
    status          TEXT   NOT NULL DEFAULT 'active',  -- active | paused
    last_msg_id     BIGINT DEFAULT 0,              -- курсор: последнее просмотренное
    hits_count      INT    DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_kw_watchers_owner  ON keyword_watchers(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_kw_watchers_active ON keyword_watchers(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS keyword_hits (
    id              BIGSERIAL PRIMARY KEY,
    watcher_id      BIGINT NOT NULL REFERENCES keyword_watchers(id) ON DELETE CASCADE,
    owner_id        BIGINT NOT NULL,
    chat_ref        TEXT,
    message_id      BIGINT NOT NULL,
    from_user_id    BIGINT,
    from_username   TEXT,
    matched_keyword TEXT,
    text            TEXT,
    caught_at       TIMESTAMPTZ DEFAULT now(),
    delivered       BOOLEAN DEFAULT FALSE,
    UNIQUE (watcher_id, message_id)   -- одно сообщение → один лид (идемпотентность)
);
CREATE INDEX IF NOT EXISTS idx_kw_hits_owner ON keyword_hits(owner_id, caught_at DESC);
