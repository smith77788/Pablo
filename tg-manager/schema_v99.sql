-- v99: Bot Promotion Platform — promo_orders, bot_warehouse, smm_panels, promo_logs

CREATE TABLE IF NOT EXISTS promo_orders (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    keyword         TEXT NOT NULL,
    target_position INT NOT NULL DEFAULT 1,
    bot_id          INT,
    status          TEXT NOT NULL DEFAULT 'waiting',
    -- waiting | aging | boosting | topup | checking | topped | transferred | cancelled
    smm_panel_id    INT,
    smm_order_id    TEXT,
    target_subs     INT,
    current_subs    INT DEFAULT 0,
    last_position   INT,
    notify_on_top   BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS bot_warehouse (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    bot_username    TEXT NOT NULL,
    bot_token_enc   TEXT,
    session_path    TEXT,
    account_id      INT,
    proxy_id        INT,
    status          TEXT NOT NULL DEFAULT 'aging',
    -- aging | ready | working | topped | banned | transferred
    registered_at   TIMESTAMPTZ,
    ready_at        TIMESTAMPTZ,
    current_subs    INT DEFAULT 0,
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS smm_panels (
    id              SERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL,
    name            TEXT NOT NULL,
    api_url         TEXT NOT NULL,
    api_key_enc     TEXT NOT NULL,
    service_id      TEXT DEFAULT '',
    is_active       BOOLEAN DEFAULT TRUE,
    balance         NUMERIC(10,2) DEFAULT 0,
    last_checked    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promo_logs (
    id              BIGSERIAL PRIMARY KEY,
    order_id        INT,
    owner_id        BIGINT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'INFO',
    event           TEXT NOT NULL,
    message         TEXT NOT NULL,
    meta            JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS promo_orders_owner_idx  ON promo_orders(owner_id);
CREATE INDEX IF NOT EXISTS promo_orders_status_idx ON promo_orders(status);
CREATE INDEX IF NOT EXISTS bot_warehouse_owner_idx  ON bot_warehouse(owner_id);
CREATE INDEX IF NOT EXISTS bot_warehouse_status_idx ON bot_warehouse(status);
CREATE INDEX IF NOT EXISTS smm_panels_owner_idx    ON smm_panels(owner_id);
CREATE INDEX IF NOT EXISTS promo_logs_order_idx    ON promo_logs(order_id);
CREATE INDEX IF NOT EXISTS promo_logs_owner_idx    ON promo_logs(owner_id);
CREATE INDEX IF NOT EXISTS promo_logs_created_idx  ON promo_logs(created_at DESC);
