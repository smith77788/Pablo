-- Schema v61: Система сбора отчётов об ошибках с сохранением скриншотов

CREATE TABLE IF NOT EXISTS error_reports (
    id              SERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL,
    description     TEXT NOT NULL,          -- описание ошибки от пользователя
    screenshot_id   TEXT,                   -- file_id скриншота из Telegram
    screenshot_path TEXT,                   -- путь где сохранён скриншот
    device_info     JSONB,                  -- информация о устройстве/ПО (опционально)
    context         JSONB,                  -- контекст: текущее меню, что делал (опционально)
    status          VARCHAR(20) NOT NULL DEFAULT 'new',  -- new | viewing | fixing | fixed | duplicate
    assignee_id     BIGINT,                 -- кому назначена (для admin-tracking)
    notes           TEXT,                   -- мои заметки при анализе
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_error_reports_user
    ON error_reports(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_error_reports_status
    ON error_reports(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_error_reports_new
    ON error_reports(created_at DESC)
    WHERE status IN ('new', 'viewing');
