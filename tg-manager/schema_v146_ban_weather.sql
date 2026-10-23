-- schema_v146_ban_weather.sql — Ban Weather / Fleet Immune System (Фаза 1: фундамент)
-- Иммутабельный лог исходов + автопсия. См. docs/BAN_WEATHER_MODULE.md.
-- Всё идемпотентно (IF NOT EXISTS / CREATE OR REPLACE). Зависит только от
-- tg_accounts (существует задолго до этой версии) — порядок применения неважен.

-- ── Лог смен статуса аккаунта (источник правды исходов) ──────────────────────
CREATE TABLE IF NOT EXISTS account_status_events (
    id           BIGSERIAL PRIMARY KEY,
    acc_id       BIGINT NOT NULL,
    owner_id     BIGINT,
    old_status   TEXT,
    new_status   TEXT NOT NULL,
    is_death     BOOLEAN NOT NULL DEFAULT FALSE,
    reason       TEXT,               -- обогащение (account_status.set_status)
    source       TEXT,               -- какой код сменил статус
    context      JSONB,              -- доп. контекст на момент смены
    autopsy      JSONB,              -- отчёт вскрытия (immunity_engine, async)
    signature    TEXT,               -- сигнатура смерти (immunity_engine)
    processed_at TIMESTAMPTZ,        -- NULL = ещё не обработано движком
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ase_owner_created ON account_status_events(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ase_unprocessed ON account_status_events(created_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ase_death_sig ON account_status_events(signature, created_at DESC) WHERE is_death;
CREATE INDEX IF NOT EXISTS idx_ase_acc ON account_status_events(acc_id, created_at DESC);

-- ── Триггер: гарантированный захват ЛЮБОЙ смены acc_status ────────────────────
-- Ловит все ~13 точек смены статуса в коде (и будущие) без правок горячих файлов.
CREATE OR REPLACE FUNCTION _immunity_capture_status() RETURNS trigger AS $$
BEGIN
    IF NEW.acc_status IS DISTINCT FROM OLD.acc_status THEN
        INSERT INTO account_status_events
            (acc_id, owner_id, old_status, new_status, is_death, created_at)
        VALUES
            (NEW.id, NEW.owner_id, OLD.acc_status, NEW.acc_status,
             (NEW.acc_status IN ('banned','spamblock','deactivated','session_expired','frozen')),
             now());
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_immunity_capture_status ON tg_accounts;
CREATE TRIGGER trg_immunity_capture_status
    AFTER UPDATE OF acc_status ON tg_accounts
    FOR EACH ROW
    EXECUTE FUNCTION _immunity_capture_status();

-- ── Роллап-кэш сигнатур (наполняется детектором вспышек, Фаза 2) ─────────────
CREATE TABLE IF NOT EXISTS immunity_signatures (
    signature    TEXT NOT NULL,
    owner_id     BIGINT,             -- NULL = глобально (cross-fleet, Фаза 5)
    deaths_1h    INT NOT NULL DEFAULT 0,
    deaths_24h   INT NOT NULL DEFAULT 0,
    exposures_24h INT NOT NULL DEFAULT 0,   -- аккаунты с этой сигнатурой, что НЕ умерли
    death_rate   NUMERIC NOT NULL DEFAULT 0,
    threat_level TEXT NOT NULL DEFAULT 'calm',  -- calm/watch/warning/storm
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (signature, owner_id)
);
CREATE INDEX IF NOT EXISTS idx_imsig_threat ON immunity_signatures(owner_id, threat_level, updated_at DESC);

-- ── Правила «не повторять» (из автопсий или вручную) ─────────────────────────
CREATE TABLE IF NOT EXISTS immunity_rules (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    match        JSONB NOT NULL,      -- спецификация совпадения (op_type/geo/subnet/...)
    action       TEXT NOT NULL DEFAULT 'throttle',  -- throttle/bench/quarantine/block
    source       TEXT NOT NULL DEFAULT 'manual',    -- autopsy/manual
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    cooldown_sec INT NOT NULL DEFAULT 3600,
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_imrules_owner ON immunity_rules(owner_id, enabled);

-- ── Активные предохранители (эфемерные, с cooldown) — Фаза 3 ─────────────────
CREATE TABLE IF NOT EXISTS immunity_policy (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    op_type      TEXT,                -- NULL = все типы
    signature    TEXT,                -- NULL = по op_type
    action       TEXT NOT NULL,       -- throttle/bench/quarantine/block
    reason       TEXT NOT NULL,       -- человекочитаемая причина (объяснимость)
    until        TIMESTAMPTZ NOT NULL,-- конец cooldown; авто-снятие после
    auto         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_impolicy_active ON immunity_policy(owner_id, until);
