-- Gift Transfer Manager Schema (v77)
-- Adds tables for gift inventory, transfer plans, operations, and reports

-- ─────────────────────────────────────────────────────────────────────────
-- Saved recipients for gift transfers
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_recipients (
    id              BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,          -- Display name (e.g., "Main Admin", "John Doe")
    username        TEXT,                   -- Telegram @username or empty for user ID
    user_id         BIGINT,                -- Telegram user ID if known
    is_main_admin   BOOLEAN DEFAULT FALSE,  -- Flag for main admin recipient
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(owner_id, name)
);

CREATE INDEX IF NOT EXISTS idx_gift_recipients_owner ON gift_recipients(owner_id);

-- ─────────────────────────────────────────────────────────────────────────
-- Gift inventory cache — scanned gifts from connected accounts
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_inventory (
    id                  BIGSERIAL PRIMARY KEY,
    owner_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    account_id          BIGINT NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    gift_id             TEXT NOT NULL,       -- Telegram gift ID (for transfer)
    gift_type           TEXT NOT NULL,       -- Gift type name (e.g., "Basic Star", "Limited Edition")
    slug                TEXT,                -- Gift slug for API calls
    stars_cost          INTEGER,             -- Cost in Telegram Stars (if known)
    is_transferable     BOOLEAN DEFAULT TRUE,-- Can be transferred
    is_premium          BOOLEAN DEFAULT FALSE,
    is_unique            BOOLEAN DEFAULT FALSE,
    is_limited          BOOLEAN DEFAULT FALSE,
    limited_count       INTEGER,             -- For limited edition: total count
    first_owner         BOOLEAN DEFAULT FALSE,-- Was first owner
    generation          INTEGER,             -- Generation number
    added_at            TIMESTAMPTZ DEFAULT now(),
    last_seen_at        TIMESTAMPTZ DEFAULT now(),
    UNIQUE(account_id, gift_id)
);

CREATE INDEX IF NOT EXISTS idx_gift_inventory_owner ON gift_inventory(owner_id);
CREATE INDEX IF NOT EXISTS idx_gift_inventory_account ON gift_inventory(account_id);
CREATE INDEX IF NOT EXISTS idx_gift_inventory_transferable ON gift_inventory(owner_id, is_transferable);

-- ─────────────────────────────────────────────────────────────────────────
-- Gift transfer plans
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_transfer_plans (
    id                  BIGSERIAL PRIMARY KEY,
    owner_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                TEXT,                -- Optional plan name
    recipient_username  TEXT,                -- @username or empty
    recipient_user_id   BIGINT,              -- Telegram user ID
    recipient_name      TEXT,                -- Display name ("Main Admin", etc.)
    payment_source      TEXT NOT NULL,       -- 'stars', 'wallet', 'saved_method', 'auto'
    payment_method_id   BIGINT,              -- Link to saved payment method if any
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending/validated/preview/queued/running/done/cancelled
    total_gifts         INTEGER DEFAULT 0,
    selected_gifts      INTEGER DEFAULT 0,
    estimated_cost      BIGINT DEFAULT 0,    -- Total estimated cost in stars
    actual_cost         BIGINT DEFAULT 0,     -- Actual cost after execution
    error_message       TEXT,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gift_transfer_plans_owner ON gift_transfer_plans(owner_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_plans_status ON gift_transfer_plans(owner_id, status);

-- ─────────────────────────────────────────────────────────────────────────
-- Gift transfer items — individual gifts in a plan
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_transfer_items (
    id                  BIGSERIAL PRIMARY KEY,
    plan_id             BIGINT NOT NULL REFERENCES gift_transfer_plans(id) ON DELETE CASCADE,
    inventory_id        BIGINT NOT NULL REFERENCES gift_inventory(id) ON DELETE CASCADE,
    account_id          BIGINT NOT NULL REFERENCES tg_accounts(id) ON DELETE CASCADE,
    gift_id             TEXT NOT NULL,
    gift_type           TEXT NOT NULL,
    stars_cost          INTEGER DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending/queued/transferred/failed/skipped/pending_confirmation
    error_message       TEXT,
    error_code          TEXT,                -- failure category
    is_retryable        BOOLEAN DEFAULT TRUE,
    retry_count         INTEGER DEFAULT 0,
    max_retries         INTEGER DEFAULT 3,
    transferred_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE(plan_id, inventory_id)
);

CREATE INDEX IF NOT EXISTS idx_gift_transfer_items_plan ON gift_transfer_items(plan_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_items_status ON gift_transfer_items(plan_id, status);

-- ─────────────────────────────────────────────────────────────────────────
-- Gift transfer operations (linked to operation_queue)
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_transfer_ops (
    id                  BIGSERIAL PRIMARY KEY,
    plan_id             BIGINT NOT NULL REFERENCES gift_transfer_plans(id) ON DELETE CASCADE,
    operation_id        BIGINT NOT NULL REFERENCES operation_queue(id) ON DELETE CASCADE,
    total_items         INTEGER DEFAULT 0,
    transferred         INTEGER DEFAULT 0,
    failed              INTEGER DEFAULT 0,
    skipped             INTEGER DEFAULT 0,
    pending_confirmation INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gift_transfer_ops_plan ON gift_transfer_ops(plan_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_ops_operation ON gift_transfer_ops(operation_id);

-- ─────────────────────────────────────────────────────────────────────────
-- Gift transfer reports — final reports for each operation
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gift_transfer_reports (
    id                  BIGSERIAL PRIMARY KEY,
    plan_id             BIGINT NOT NULL REFERENCES gift_transfer_plans(id) ON DELETE CASCADE,
    operation_id        BIGINT,             -- May be NULL if operation was never created
    owner_id            BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- Summary stats
    total_gifts_found   INTEGER DEFAULT 0,
    total_selected      INTEGER DEFAULT 0,
    transferred         INTEGER DEFAULT 0,
    failed              INTEGER DEFAULT 0,
    skipped             INTEGER DEFAULT 0,
    pending_confirmation INTEGER DEFAULT 0,
    
    -- Cost info
    total_cost          BIGINT DEFAULT 0,
    currency            TEXT DEFAULT 'stars',
    
    -- Recipient info
    recipient_username  TEXT,
    recipient_user_id   BIGINT,
    recipient_name      TEXT,
    
    -- Account info
    accounts_used       INTEGER DEFAULT 0,
    accounts_data       JSONB,              -- Array of {account_id, phone, gifts_count}
    
    -- Failure details
    error_summary       JSONB,              -- {category: [list of gift types]}
    retryable_failures  JSONB,              -- List of failed item IDs
    non_retryable       JSONB,
    
    -- Recommendations
    next_actions        JSONB,              -- Suggested actions after report
    
    -- Timestamps
    created_at          TIMESTAMPTZ DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gift_transfer_reports_owner ON gift_transfer_reports(owner_id);
CREATE INDEX IF NOT EXISTS idx_gift_transfer_reports_plan ON gift_transfer_reports(plan_id);