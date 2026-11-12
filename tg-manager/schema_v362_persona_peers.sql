-- schema_v362: персона помнит собеседников.
--
-- Память персоны была только о себе: что она говорила и что рассказывала о
-- своей жизни. С КЕМ она говорила, не помнил никто — человек, с которым
-- аккаунт вчера спорил в чате, сегодня получал ответ как незнакомец, а
-- черновик ответа в личке не знал, что персона ему уже обещала. Здесь —
-- по строке на пару «персона — человек»: хвост переписки дословно и заметка
-- о человеке, которую модель пересобирает раз в несколько обменов.
CREATE TABLE IF NOT EXISTS persona_peers (
    id BIGSERIAL PRIMARY KEY,
    persona_id BIGINT NOT NULL REFERENCES persona_profiles(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    peer_tg_id BIGINT NOT NULL,
    peer_name VARCHAR(80) NOT NULL DEFAULT '',
    last_where VARCHAR(255) NOT NULL DEFAULT '',
    lines TEXT[] NOT NULL DEFAULT '{}',
    exchanges INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    notes_at TIMESTAMPTZ,
    notes_exchanges INTEGER NOT NULL DEFAULT 0,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (persona_id, peer_tg_id)
);
CREATE INDEX IF NOT EXISTS idx_persona_peers_recent ON persona_peers(persona_id, last_seen DESC);
