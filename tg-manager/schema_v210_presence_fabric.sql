-- Ткань присутствия (presence-fabric): декларативная самовосстанавливающаяся
-- контрольная плоскость для телеграм-присутствия.
--
-- Архитектурный сдвиг: РАЗДЕЛЕНИЕ логической личности и физического аккаунта.
-- Личность — durable-состояние (имя/аватар/персона/память). Физический аккаунт
-- (tg_accounts) — расходное «тело», на которое личность МАТЕРИАЛИЗУЕТСЯ. Тело
-- умерло (бан/деактивация/карантин) — реконсайлер переносит личность на свежее
-- тело, сохраняя непрерывность. Аккаунты — «скот», личности — «питомцы».

-- Логическая личность.
CREATE TABLE IF NOT EXISTS presence_identities (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    avatar_emoji    TEXT NOT NULL DEFAULT '🧑',
    persona_id      BIGINT,                       -- голос/поведение (bot_sales_personas), опц.
    acc_id          BIGINT,                        -- ТЕКУЩЕЕ тело (tg_accounts.id); NULL = не материализована
    status          TEXT NOT NULL DEFAULT 'active', -- active|paused
    memory          JSONB NOT NULL DEFAULT '{}',   -- durable-состояние личности
    materialized_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_presence_identities_owner ON presence_identities(owner_id);

-- Желаемое членство (declarative desired-state): личность ДОЛЖНА быть в чате.
CREATE TABLE IF NOT EXISTS presence_targets (
    id                 BIGSERIAL PRIMARY KEY,
    identity_id        BIGINT NOT NULL REFERENCES presence_identities(id) ON DELETE CASCADE,
    owner_id           BIGINT NOT NULL,
    chat_ref           TEXT NOT NULL,             -- @username / инвайт-ссылка / id
    state              TEXT NOT NULL DEFAULT 'desired', -- desired|converging|present|lost
    last_op_id         BIGINT,                    -- операция конвергенции (bulk_join)
    last_reconciled_at TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(identity_id, chat_ref)
);
CREATE INDEX IF NOT EXISTS idx_presence_targets_owner ON presence_targets(owner_id);
CREATE INDEX IF NOT EXISTS idx_presence_targets_identity ON presence_targets(identity_id);

-- Event-sourced лог: каждое наблюдение/действие реконсайлера (аудит + проекция).
CREATE TABLE IF NOT EXISTS presence_events (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    identity_id BIGINT,
    kind        TEXT NOT NULL,                    -- materialized|rematerialized|no_body|
                                                   -- target_converging|target_present|target_lost|drift
    detail      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_presence_events_owner ON presence_events(owner_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_presence_events_identity ON presence_events(identity_id, id DESC);
