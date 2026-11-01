-- schema_v263: темп флота и обучаемое состояние аккаунтов — в базе, а не в
-- памяти процесса.
--
-- До этой миграции оба жили только в памяти исполнителя. Каждый деплой
-- обнулял накопленную осторожность (первые действия после выпуска шли
-- базовым темпом поверх флуда, пойманного минуту назад), а второй процесс не
-- видел ни флудов соседа, ни выученных пауз. Пишет и читает
-- services/pacing_store.py.

-- Исходы операций по флоту — окно для множителя темпа (pacing_engine).
-- Решения смотрят на последние десятки строк; хвост чистит db_maintenance.
CREATE TABLE IF NOT EXISTS fleet_pacing_events (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type TEXT NOT NULL DEFAULT '',
    ok          BOOLEAN NOT NULL DEFAULT FALSE,
    flood       BOOLEAN NOT NULL DEFAULT FALSE,
    ban         BOOLEAN NOT NULL DEFAULT FALSE,
    worker_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_fleet_pacing_events_created
    ON fleet_pacing_events (created_at);

-- Обучаемое состояние аккаунта (flood_engine._AccountFloodState): серия
-- флудов подряд, риск, выученные паузы по типам действий, время последнего
-- флуда. Пауза аккаунта (cooldown_until) здесь НЕ дублируется: она уже
-- живёт в tg_accounts и пишется сразу.
CREATE TABLE IF NOT EXISTS account_pacing_state (
    account_id          BIGINT PRIMARY KEY,
    consecutive_floods  INTEGER NOT NULL DEFAULT 0,
    risk_score          REAL NOT NULL DEFAULT 0,
    action_delays       JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_flood_at       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
