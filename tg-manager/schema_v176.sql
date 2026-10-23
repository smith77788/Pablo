-- Связки 2c: кросспостинг. Нативного API у Telegram нет — реализуем как правила
-- пересылки: новые посты источника форвардятся в цель. Запуск — по требованию/
-- расписанию (op crosspost_run), не всегда-он-поллер (безопаснее по сессиям).
CREATE TABLE IF NOT EXISTS crosspost_links (
    id                 BIGSERIAL PRIMARY KEY,
    owner_id           BIGINT NOT NULL,
    source_channel_id  BIGINT NOT NULL,
    target_channel_id  BIGINT NOT NULL,
    account_id         BIGINT,              -- аккаунт-форвардер (админ обоих); NULL → подобрать
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    last_msg_id        BIGINT NOT NULL DEFAULT 0,   -- курсор: последний пересланный пост
    forwarded_total    BIGINT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at        TIMESTAMPTZ,
    UNIQUE (owner_id, source_channel_id, target_channel_id)
);
CREATE INDEX IF NOT EXISTS idx_crosspost_owner ON crosspost_links(owner_id, enabled);
