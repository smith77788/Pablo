-- schema_v45: account_health_history — снапшоты health_score для трендов
-- Используется account_health.py (сохранение) и health_dashboard.py (отображение)

CREATE TABLE IF NOT EXISTS account_health_history (
    id              BIGSERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    owner_id        BIGINT NOT NULL,
    health_score    NUMERIC(5,2) NOT NULL DEFAULT 0,  -- 0.00–100.00
    load_score      NUMERIC(5,2) NOT NULL DEFAULT 0,  -- 0.00–100.00
    trust_score     NUMERIC(5,4) NOT NULL DEFAULT 0,  -- сырой trust_score из tg_accounts (0–1)
    flood_events_7d INTEGER NOT NULL DEFAULT 0,
    success_ops     INTEGER NOT NULL DEFAULT 0,
    fail_ops        INTEGER NOT NULL DEFAULT 0,
    warmup_state    TEXT NOT NULL DEFAULT 'raw',       -- raw/warming/ready/veteran
    suitability     JSONB DEFAULT '{}',               -- {"invite":true,"dm":true,...}
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_hist_account ON account_health_history(account_id, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_health_hist_owner   ON account_health_history(owner_id, recorded_at DESC);

-- Авто-очистка старых снапшотов (> 30 дней)
-- Вызывается из account_health.py при каждом цикле
