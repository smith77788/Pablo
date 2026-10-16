-- Личный список каналов, которые аккаунт нашёл САМ во время прогрева.
--
-- Зачем: пул каналов прогрева — двадцать один @канал, зашитый в код, один на
-- весь флот. Сколько бы аккаунтов ни грелось, все ходят по одним и тем же
-- адресам; совпадающий граф интересов кластеризует когорту ровно так же, как
-- совпадающий граф вступлений, от которого мы уже уходили.
--
-- Действия «похожие каналы» (channels.getChannelRecommendations), поиск и
-- переход по ссылке из поста приносят каналы, зависящие от того, что аккаунт
-- уже читал, — то есть у каждого аккаунта свои. Они складываются сюда, и круг
-- каналов аккаунта растёт сам, как у живого человека.
--
-- Почему в базе, а не в памяти процесса: реплик может быть больше одной, и
-- список должен пережить рестарт (интересы человека не обнуляются деплоем).

CREATE TABLE IF NOT EXISTS account_warmup_interests (
    account_id   BIGINT      NOT NULL,
    channel_ref  TEXT        NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'discovery',  -- discovery | search | link
    seen_count   INT         NOT NULL DEFAULT 0,
    found_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, channel_ref)
);

-- Выборка всегда «свежие сначала» и всегда по аккаунту.
CREATE INDEX IF NOT EXISTS idx_warmup_interests_acc
    ON account_warmup_interests(account_id, last_seen_at DESC NULLS LAST, found_at DESC);
