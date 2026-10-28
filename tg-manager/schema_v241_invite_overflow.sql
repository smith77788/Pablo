-- v241: переливание инвайта по резерву каналов.
--
-- Кампания инвайта может идти не в один канал, а по цепочке: главный канал,
-- затем пустые каналы из резерва владельца, когда предыдущий упёрся в лимит
-- приглашённых или словил флуд. Таблица хранит цепочку: какие каналы взяты,
-- какой сейчас активный, сколько в него приглашено, оформлен ли он.
--
-- chain_key — нормализованная ссылка главного канала (тот же ключ, что у
-- дедупа инвайта): продолжение на следующий день начинает с активного канала
-- цепочки, а не снова с переполненного главного. Канал резерва, попавший в
-- одну цепочку, другим кампаниям как «пустой» больше не выдаётся.
CREATE TABLE IF NOT EXISTS invite_overflow_channels (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    chain_key   TEXT NOT NULL,
    channel_ref TEXT NOT NULL,          -- ссылка, по которой приглашают (@username / t.me/+…)
    channel_id  BIGINT,                 -- id канала резерва (у главного может быть NULL)
    status      TEXT NOT NULL DEFAULT 'active',   -- active|full|burned
    prepared    BOOLEAN NOT NULL DEFAULT FALSE,   -- оформление (описание/пост) применено
    invited_ok  INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',
    op_id       BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, chain_key, channel_ref)
);
CREATE INDEX IF NOT EXISTS idx_invite_overflow_owner_channel
    ON invite_overflow_channels(owner_id, channel_id);
