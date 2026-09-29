-- Виртуальный администратор канала — настройки и черновики на КАЖДЫЙ канал.
--
-- До этой миграции у администратора были только общие «Правила редактора»
-- (va_channel_brain под ключом '*'): он проверял чужие посты, но сам канал не
-- вёл. Здесь — то, что делает его администратором конкретного канала:
-- тематика, аудитория, голос, расписание, режим публикации. Рубрики и правила
-- бренда канала живут в va_channel_brain под channel_key = channel_id::text.
--
-- Тематику задаёт владелец (или принимает предложенную по названию канала), а не
-- выводит из истории постов, поэтому пустой канал администрируется с первого дня.
-- Только ADD/CREATE, идемпотентно.

CREATE TABLE IF NOT EXISTS va_channel_admin (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT      NOT NULL,
    channel_id     BIGINT      NOT NULL,          -- managed_channels.channel_id
    enabled        BOOLEAN     NOT NULL DEFAULT FALSE,
    topic          TEXT        NOT NULL DEFAULT '',  -- о чём канал
    audience       TEXT        NOT NULL DEFAULT '',  -- для кого
    tone           TEXT        NOT NULL DEFAULT '',  -- голос канала
    notes          TEXT        NOT NULL DEFAULT '',  -- что обязательно / что нельзя
    project_info   TEXT        NOT NULL DEFAULT '',  -- о бизнесе своими словами (владелец)
    lead_contact   TEXT        NOT NULL DEFAULT '',  -- куда вести клиента: @менеджер, ссылка
    brief          JSONB       NOT NULL DEFAULT '{}'::jsonb, -- разбор ниши: оффер, боли, возражения
    posts_per_day  INTEGER     NOT NULL DEFAULT 2 CHECK (posts_per_day BETWEEN 1 AND 12),
    window_start   INTEGER     NOT NULL DEFAULT 9  CHECK (window_start BETWEEN 0 AND 23),
    window_end     INTEGER     NOT NULL DEFAULT 21 CHECK (window_end BETWEEN 1 AND 24),
    tz_offset      INTEGER     NOT NULL DEFAULT 3  CHECK (tz_offset BETWEEN -12 AND 14),
    publish_mode   TEXT        NOT NULL DEFAULT 'auto'
                   CHECK (publish_mode IN ('review','auto')),
    intro_pending  BOOLEAN     NOT NULL DEFAULT TRUE,  -- начать с поста-знакомства
    next_post_at   TIMESTAMPTZ,
    last_post_at   TIMESTAMPTZ,
    last_error     TEXT,
    fail_streak    INTEGER     NOT NULL DEFAULT 0,   -- сбоев подряд (самолечение)
    setup_done     BOOLEAN     NOT NULL DEFAULT FALSE, -- профиль и план собраны
    last_op_id     BIGINT,                             -- последняя публикация
    alerted_at     TIMESTAMPTZ,                        -- когда звали владельца
    auto_tune      BOOLEAN     NOT NULL DEFAULT TRUE, -- доли рубрик учатся на статистике
    members_count  INTEGER,
    last_stats_at  TIMESTAMPTZ,
    last_report_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_va_channel_admin_due
    ON va_channel_admin(next_post_at) WHERE enabled;

CREATE TABLE IF NOT EXISTS va_admin_drafts (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    channel_id  BIGINT      NOT NULL,
    pillar      TEXT,
    body        TEXT        NOT NULL,
    reasons     JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- замечания редактора
    status      TEXT        NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','published','rejected','expired','failed')),
    is_intro    BOOLEAN     NOT NULL DEFAULT FALSE,
    op_id       BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_va_admin_drafts_owner_status
    ON va_admin_drafts(owner_id, status, created_at DESC);

-- Статистика постов администратора: без msg_id нельзя спросить у Telegram
-- просмотры конкретного поста, а без просмотров контент-микс не учится.
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS msg_id    BIGINT;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS views     INTEGER;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS forwards  INTEGER;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS reactions INTEGER;
ALTER TABLE va_channel_posts ADD COLUMN IF NOT EXISTS stats_at  TIMESTAMPTZ;

-- Контент-план наперёд: слот → рубрика + тема поста. Администратор пишет пост
-- по пункту плана, когда подходит его время, и сам достраивает план.
CREATE TABLE IF NOT EXISTS va_admin_plan (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    channel_id  BIGINT      NOT NULL,
    slot_at     TIMESTAMPTZ NOT NULL,
    pillar      TEXT        NOT NULL DEFAULT '',
    topic       TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned','done','skipped')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_va_admin_plan_channel
    ON va_admin_plan(owner_id, channel_id, status, slot_at);

-- Рост канала по дням (подписчики) — для отчёта и оценки курса.
CREATE TABLE IF NOT EXISTS va_channel_stats (
    owner_id       BIGINT  NOT NULL,
    channel_id     BIGINT  NOT NULL,
    day            DATE    NOT NULL,
    members_count  INTEGER NOT NULL,
    PRIMARY KEY (owner_id, channel_id, day)
);

-- Журнал администратора: что он сделал и почему. Владелец в процессе не
-- участвует, поэтому видеть работу «штата» он может только здесь.
CREATE TABLE IF NOT EXISTS va_admin_events (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    channel_id  BIGINT      NOT NULL,
    kind        TEXT        NOT NULL,
    text        TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_va_admin_events_channel
    ON va_admin_events(owner_id, channel_id, created_at DESC);

-- Уборка (services/db_maintenance) удаляет старое по created_at пачками — без
-- индекса по дате каждая пачка читала бы таблицу целиком.
CREATE INDEX IF NOT EXISTS idx_va_admin_events_created ON va_admin_events(created_at);
CREATE INDEX IF NOT EXISTS idx_va_admin_plan_created   ON va_admin_plan(created_at);
CREATE INDEX IF NOT EXISTS idx_va_admin_drafts_created ON va_admin_drafts(created_at);
