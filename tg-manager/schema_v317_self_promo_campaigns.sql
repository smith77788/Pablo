-- schema_v317: самопиар своих каналов и ботов по своей же аудитории.
--
-- Прежний «Самопиар» умел одно: разослать подписчикам ботов владельца рекламу
-- самой платформы. Продвинуть СВОЙ канал или СВОЕГО бота по своей аудитории
-- было нечем. Кампания самопиара: что продвигаем (бот, канал или ссылка), где
-- показываем (подписчики выбранных ботов в личку и/или пост в своих каналах),
-- кому (сегмент; без тех, кто уже в продвигаемом боте; не чаще раза в
-- cooldown_days одному человеку) и как часто (вручную или раз в auto_days дней
-- новым подписчикам, которые эту кампанию ещё не получали).
CREATE TABLE IF NOT EXISTS self_promo_campaigns (
    id                   BIGSERIAL PRIMARY KEY,
    owner_id             BIGINT      NOT NULL,
    title                TEXT        NOT NULL,
    target_kind          TEXT        NOT NULL CHECK (target_kind IN ('bot', 'channel', 'link')),
    target_ref           BIGINT,
    target_url           TEXT        NOT NULL,
    body                 TEXT        NOT NULL,
    button_text          TEXT        NOT NULL DEFAULT '',
    bot_ids              BIGINT[]    NOT NULL DEFAULT '{}',
    channel_ids          BIGINT[]    NOT NULL DEFAULT '{}',
    segment              TEXT        NOT NULL DEFAULT 'all',
    skip_target_audience BOOLEAN     NOT NULL DEFAULT TRUE,
    cooldown_days        INT         NOT NULL DEFAULT 7 CHECK (cooldown_days BETWEEN 0 AND 90),
    auto_days            INT         NOT NULL DEFAULT 0 CHECK (auto_days BETWEEN 0 AND 90),
    next_auto_at         TIMESTAMPTZ,
    deep_link_id         INT,
    status               TEXT        NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'archived')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_self_promo_campaigns_owner
    ON self_promo_campaigns(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_self_promo_campaigns_auto
    ON self_promo_campaigns(next_auto_at) WHERE auto_days > 0 AND status = 'active';

-- Запуск кампании: что было поставлено в очередь (для статистики и истории).
CREATE TABLE IF NOT EXISTS self_promo_launches (
    id            BIGSERIAL PRIMARY KEY,
    campaign_id   BIGINT      NOT NULL REFERENCES self_promo_campaigns(id) ON DELETE CASCADE,
    owner_id      BIGINT      NOT NULL,
    trigger       TEXT        NOT NULL DEFAULT 'manual',
    recipients    INT         NOT NULL DEFAULT 0,
    channel_posts INT         NOT NULL DEFAULT 0,
    broadcast_ids BIGINT[]    NOT NULL DEFAULT '{}',
    op_ids        BIGINT[]    NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_self_promo_launches_campaign
    ON self_promo_launches(campaign_id, created_at DESC);

-- Кому кампания уже ушла. Первичный ключ (кампания, человек) — это и есть
-- защита от второй отправки: строка занимается ДО постановки рассылки, и
-- одновременные запуски не делят одного человека. По sent_at считается пауза
-- «не чаще раза в N дней» между ЛЮБЫМИ кампаниями самопиара владельца.
CREATE TABLE IF NOT EXISTS self_promo_sends (
    campaign_id BIGINT      NOT NULL REFERENCES self_promo_campaigns(id) ON DELETE CASCADE,
    user_id     BIGINT      NOT NULL,
    owner_id    BIGINT      NOT NULL,
    bot_id      BIGINT      NOT NULL,
    launch_id   BIGINT,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_self_promo_sends_owner_user
    ON self_promo_sends(owner_id, user_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_self_promo_sends_launch
    ON self_promo_sends(launch_id);
