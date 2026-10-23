-- WB Chat — аккаунтная автоматизация (по образцу Telegram-стека Infragram).
--
-- Зеркалит модель Telegram: пользовательские аккаунты с сессиями/прокси/здоровьем,
-- очередь операций с воркером и ротацией аккаунтов, цели массовых действий и
-- дневные бюджеты. Транспорт WB Chat пока за абстракцией (services/wb_chat/
-- transport.py); эти таблицы от драйвера не зависят.
--
-- Применяется загрузчиком schema*.sql (database/db.py) в порядке версии → v185
-- после v184. Всё идемпотентно (IF NOT EXISTS).

-- ── Аккаунты WB Chat ─────────────────────────────────────────────────────────
-- Один аккаунт = один вход по номеру (WB ID). Сессия и прокси хранятся
-- ЗАШИФРОВАННЫМИ (token_vault, как сессии Telegram) — в открытом виде секреты
-- не кладём. status/health/cooldown ведут ротацию и «здоровье» флота.
CREATE TABLE IF NOT EXISTS wb_accounts (
    id            BIGSERIAL PRIMARY KEY,
    owner_id      BIGINT NOT NULL,
    phone         TEXT NOT NULL,
    wb_id         TEXT,                                 -- идентификатор WB ID
    user_id       TEXT,                                 -- id аккаунта в WB Chat (из get_me)
    name          TEXT,
    session_enc   TEXT,                                 -- ENC:* строка сессии (token_vault)
    proxy_enc     TEXT,                                 -- ENC:* прокси (socks5://…), NULL → без прокси
    device        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- отпечаток устройства
    status        TEXT NOT NULL DEFAULT 'new',          -- new|active|flood|banned|invalid
    health_score  INTEGER NOT NULL DEFAULT 100,         -- 0..100, снижается на ошибках
    cooldown_until TIMESTAMPTZ,                          -- не трогать до этого времени
    last_used_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Один номер на владельца — повторный вход обновляет строку, а не плодит дубль.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wb_accounts_owner_phone
    ON wb_accounts (owner_id, phone);

-- Выбор аккаунта для операции: активные, вне кулдауна, по «здоровью» и давности.
CREATE INDEX IF NOT EXISTS idx_wb_accounts_pick
    ON wb_accounts (owner_id, status, health_score DESC, last_used_at ASC NULLS FIRST);

-- ── Очередь операций ─────────────────────────────────────────────────────────
-- Массовое действие ставится в очередь и исполняется воркером (services/wb_chat/
-- op_worker.py). payload — параметры (тип-специфичные); progress/total — для UI.
CREATE TABLE IF NOT EXISTS wb_operations (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    op_type     TEXT NOT NULL,                          -- 'mass_dm' | 'bulk_join' | …
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'queued',         -- queued|running|done|failed|cancelled
    progress    INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    result      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Воркер забирает самую старую queued-операцию.
CREATE INDEX IF NOT EXISTS idx_wb_operations_queued
    ON wb_operations (status, created_at)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_wb_operations_owner
    ON wb_operations (owner_id, created_at DESC);

-- ── Цели операции ────────────────────────────────────────────────────────────
-- Разворачивание списка целей массового действия в строки — чтобы прогресс был
-- перезапускаемым (после сбоя доделываем pending, не трогая done).
CREATE TABLE IF NOT EXISTS wb_operation_targets (
    id          BIGSERIAL PRIMARY KEY,
    op_id       BIGINT NOT NULL REFERENCES wb_operations(id) ON DELETE CASCADE,
    ref         TEXT NOT NULL,                          -- ссылка/номер/username адресата
    status      TEXT NOT NULL DEFAULT 'pending',        -- pending|done|failed|skipped
    account_id  BIGINT REFERENCES wb_accounts(id) ON DELETE SET NULL,  -- каким аккаунтом сделано
    error       TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Идемпотентность разворачивания: одна цель на операцию.
CREATE UNIQUE INDEX IF NOT EXISTS idx_wb_targets_op_ref
    ON wb_operation_targets (op_id, ref);

-- Выборка «что ещё не сделано» по операции.
CREATE INDEX IF NOT EXISTS idx_wb_targets_pending
    ON wb_operation_targets (op_id, status);

-- ── Дневные бюджеты действий ────────────────────────────────────────────────
-- Ограничение действий на аккаунт в сутки (как account_budget у Telegram):
-- защищает от банов за перебор. Счётчик по (аккаунт, дата, тип действия).
CREATE TABLE IF NOT EXISTS wb_account_budget (
    account_id   BIGINT NOT NULL REFERENCES wb_accounts(id) ON DELETE CASCADE,
    action_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    action_type  TEXT NOT NULL,                         -- 'dm' | 'join' | …
    used         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, action_date, action_type)
);
