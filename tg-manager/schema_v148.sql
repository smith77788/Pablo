-- schema_v148: Enterprise UCH tables
-- Identity Graph, Versioning, Trust Level, Relationships, CRM, Smart Tags, Conflicts, Bulk Ops

-- ── Contact Versioning (снимки состояния) ─────────────────────────────
CREATE TABLE IF NOT EXISTS contact_versions (
    id SERIAL PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    version_num INTEGER NOT NULL,
    snapshot JSONB NOT NULL,
    changed_fields TEXT[],
    changed_by TEXT DEFAULT 'sync',
    change_summary TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contact_versions_contact ON contact_versions(contact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_versions_num ON contact_versions(contact_id, version_num);

-- ── Contact Relationships (связи между контактами) ────────────────────
CREATE TABLE IF NOT EXISTS contact_relationships (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_a_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    contact_b_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL DEFAULT 'mutual_group',
    strength REAL DEFAULT 0.5,
    shared_groups TEXT[],
    shared_channels TEXT[],
    shared_accounts INTEGER[],
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, contact_a_id, contact_b_id, relationship_type)
);
CREATE INDEX IF NOT EXISTS idx_contact_rel_a ON contact_relationships(contact_a_id);
CREATE INDEX IF NOT EXISTS idx_contact_rel_b ON contact_relationships(contact_b_id);
CREATE INDEX IF NOT EXISTS idx_contact_rel_owner ON contact_relationships(owner_id);

-- ── Trust Level (уверенность в слиянии) ───────────────────────────────
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS trust_score REAL DEFAULT 1.0;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS merge_confidence REAL;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS persona_type TEXT DEFAULT 'personal';
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS identity_hash TEXT;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS digital_footprint JSONB DEFAULT '{}';
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ;
ALTER TABLE unified_contacts ADD COLUMN IF NOT EXISTS source_accounts_count INTEGER DEFAULT 1;

-- ── Conflict Log (конфликты данных) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_conflicts (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    source_a_id INTEGER,
    value_a TEXT,
    source_b_id INTEGER,
    value_b TEXT,
    resolution TEXT DEFAULT 'pending',
    resolved_value TEXT,
    resolved_by BIGINT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_contact_conflicts_pending ON contact_conflicts(owner_id, resolution) WHERE resolution = 'pending';
CREATE INDEX IF NOT EXISTS idx_contact_conflicts_contact ON contact_conflicts(contact_id);

-- ── Smart Tags (авто-теги) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_smart_tags (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    source TEXT DEFAULT 'rule',
    confidence REAL DEFAULT 1.0,
    rule_id INTEGER,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, contact_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_smart_tags_contact ON contact_smart_tags(contact_id);
CREATE INDEX IF NOT EXISTS idx_smart_tags_tag ON contact_smart_tags(owner_id, tag);

-- ── Smart Tag Rules ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS smart_tag_rules (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL DEFAULT 'field_match',
    conditions JSONB NOT NULL DEFAULT '{}',
    tag TEXT NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_smart_tag_rules_owner ON smart_tag_rules(owner_id);

-- ── CRM Data (последнее общение, напоминания, воронка) ────────────────
CREATE TABLE IF NOT EXISTS contact_crm (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    stage TEXT DEFAULT 'lead',
    deal_value REAL DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    last_interaction_at TIMESTAMPTZ,
    last_interaction_type TEXT,
    last_message_preview TEXT,
    next_reminder_at TIMESTAMPTZ,
    next_reminder_text TEXT,
    assigned_to BIGINT,
    custom_fields JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_crm_reminder ON contact_crm(owner_id, next_reminder_at) WHERE next_reminder_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_contact_crm_stage ON contact_crm(owner_id, stage);

-- ── CRM Activity Log ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_crm_activity (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    activity_type TEXT NOT NULL,
    description TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_crm_activity_contact ON contact_crm_activity(contact_id);

-- ── Identity Graph (цифровая личность) ────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_identity_graph (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    contact_id TEXT NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    identity_type TEXT NOT NULL,
    identity_value TEXT NOT NULL,
    is_primary BOOLEAN DEFAULT FALSE,
    confidence REAL DEFAULT 1.0,
    source TEXT DEFAULT 'sync',
    verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, contact_id, identity_type, identity_value)
);
CREATE INDEX IF NOT EXISTS idx_identity_contact ON contact_identity_graph(contact_id);
CREATE INDEX IF NOT EXISTS idx_identity_type ON contact_identity_graph(owner_id, identity_type);

-- ── Bulk Operations Log ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_bulk_ops (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    op_type TEXT NOT NULL,
    contact_ids TEXT[] NOT NULL,
    params JSONB DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    result JSONB DEFAULT '{}',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bulk_ops_owner ON contact_bulk_ops(owner_id, status);

-- ── Export Jobs ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_export_jobs (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    format TEXT NOT NULL DEFAULT 'csv',
    filter_params JSONB DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    file_url TEXT,
    record_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

-- ── Sync State per Account ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contact_sync_state (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    account_id INTEGER NOT NULL,
    last_full_sync_at TIMESTAMPTZ,
    last_partial_sync_at TIMESTAMPTZ,
    total_contacts INTEGER DEFAULT 0,
    pending_changes INTEGER DEFAULT 0,
    sync_status TEXT DEFAULT 'idle',
    last_error TEXT,
    sync_config JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(owner_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_state_owner ON contact_sync_state(owner_id);
