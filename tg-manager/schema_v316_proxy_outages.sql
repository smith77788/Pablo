-- schema_v316: история простоев из-за мёртвых прокси.
--
-- Сторож прокси говорил о смерти прокси один раз и забывал: сколько времени
-- аккаунты стояли из-за прокси, а не из-за самих себя, не знал никто. Каждая
-- подтверждённая смерть прокси с привязанными аккаунтами открывает здесь
-- строку, оживление прокси её закрывает. accounts — сколько аккаунтов
-- осталось на мёртвом прокси после автопереезда (переехавшие не простаивают).
CREATE TABLE IF NOT EXISTS proxy_outages (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    proxy_id    BIGINT NOT NULL,
    accounts    INT NOT NULL DEFAULT 0,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_proxy_outages_owner ON proxy_outages(owner_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_proxy_outages_open ON proxy_outages(proxy_id) WHERE ended_at IS NULL;
