-- schema_v114: Growth Agent — autonomous goal tracking

CREATE TABLE IF NOT EXISTS growth_goals (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    description TEXT NOT NULL,
    target_metric VARCHAR(64) NOT NULL,  -- 'subscribers', 'views', 'revenue_usd', etc.
    target_value BIGINT NOT NULL,
    current_value BIGINT DEFAULT 0,
    deadline_at TIMESTAMPTZ,
    status VARCHAR(20) DEFAULT 'active',  -- active/paused/completed/failed
    strategy VARCHAR(32) DEFAULT 'balanced',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS growth_actions (
    id BIGSERIAL PRIMARY KEY,
    goal_id BIGINT REFERENCES growth_goals(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    action_type VARCHAR(64),
    description TEXT,
    outcome VARCHAR(32),  -- queued/success/failed/skipped
    delta_value BIGINT DEFAULT 0,
    executed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS growth_reports (
    id BIGSERIAL PRIMARY KEY,
    goal_id BIGINT REFERENCES growth_goals(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    report_date DATE DEFAULT CURRENT_DATE,
    progress_pct FLOAT DEFAULT 0,
    actions_count INT DEFAULT 0,
    delta_value BIGINT DEFAULT 0,
    ai_commentary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (goal_id, report_date)
);

CREATE INDEX IF NOT EXISTS idx_growth_goals_owner_active ON growth_goals(owner_id) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_growth_goals_updated ON growth_goals(updated_at ASC NULLS FIRST) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_growth_actions_goal ON growth_actions(goal_id, executed_at DESC);
