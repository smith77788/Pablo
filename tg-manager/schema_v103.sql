-- v103: Growth Engine — расписание автопостинга + настройки пользователя

CREATE TABLE IF NOT EXISTS growth_schedule (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    seed_id      INT REFERENCES growth_content_seeds(id) ON DELETE CASCADE,
    interval_h   INT NOT NULL DEFAULT 24,
    next_run_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run_at  TIMESTAMPTZ,
    total_sent   INT DEFAULT 0,
    is_active    BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS growth_settings (
    user_id            BIGINT PRIMARY KEY,
    watermark_enabled  BOOLEAN DEFAULT FALSE,
    updated_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS growth_schedule_active_idx
    ON growth_schedule(is_active, next_run_at)
    WHERE is_active = TRUE;
