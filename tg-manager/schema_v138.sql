-- unified_contacts: единая база контактов
CREATE TABLE IF NOT EXISTS unified_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id BIGINT NOT NULL,
    telegram_user_id BIGINT,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    patronymic TEXT,
    display_name TEXT,
    phones JSONB DEFAULT '[]',
    emails JSONB DEFAULT '[]',
    company TEXT,
    position TEXT,
    websites JSONB DEFAULT '[]',
    addresses JSONB DEFAULT '[]',
    birthday DATE,
    notes TEXT DEFAULT '',
    tags TEXT[] DEFAULT '{}',
    color_label TEXT,
    is_favorite BOOLEAN DEFAULT FALSE,
    is_premium BOOLEAN DEFAULT FALSE,
    custom_avatar_url TEXT,
    importance_level INT DEFAULT 0,
    user_rating INT DEFAULT 0,
    custom_fields JSONB DEFAULT '{}',
    discovered_at TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at TIMESTAMPTZ,
    last_changed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_uc_owner ON unified_contacts(owner_id);
CREATE INDEX IF NOT EXISTS idx_uc_tg_user ON unified_contacts(owner_id, telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_uc_username ON unified_contacts(owner_id, username);
CREATE INDEX IF NOT EXISTS idx_uc_phone ON unified_contacts USING gin(phones);
CREATE INDEX IF NOT EXISTS idx_uc_tags ON unified_contacts USING gin(tags);
CREATE INDEX IF NOT EXISTS idx_uc_favorite ON unified_contacts(owner_id, is_favorite) WHERE is_favorite = TRUE;

-- contact_sources: аккаунты-источники для каждого контакта
CREATE TABLE IF NOT EXISTS contact_sources (
    id BIGSERIAL PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    account_id BIGINT NOT NULL,
    local_name TEXT,
    first_seen_at TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at TIMESTAMPTZ,
    status TEXT DEFAULT 'active',
    UNIQUE(contact_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_cs_contact ON contact_sources(contact_id);
CREATE INDEX IF NOT EXISTS idx_cs_account ON contact_sources(account_id);

-- contact_groups: пользовательские группы
CREATE TABLE IF NOT EXISTS contact_groups (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    color TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cg_owner ON contact_groups(owner_id);

-- contact_group_members: связь контактов с группами
CREATE TABLE IF NOT EXISTS contact_group_members (
    group_id BIGINT NOT NULL REFERENCES contact_groups(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, contact_id)
);

-- contact_history: история изменений
CREATE TABLE IF NOT EXISTS contact_history (
    id BIGSERIAL PRIMARY KEY,
    contact_id UUID NOT NULL REFERENCES unified_contacts(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    field_name TEXT,
    old_value TEXT,
    new_value TEXT,
    source TEXT DEFAULT 'user',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ch_contact ON contact_history(contact_id);
CREATE INDEX IF NOT EXISTS idx_ch_owner ON contact_history(owner_id);

-- contact_sync_log: лог синхронизации
CREATE TABLE IF NOT EXISTS contact_sync_log (
    id BIGSERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    account_id BIGINT NOT NULL,
    sync_type TEXT NOT NULL,
    contacts_synced INT DEFAULT 0,
    contacts_created INT DEFAULT 0,
    contacts_updated INT DEFAULT 0,
    contacts_merged INT DEFAULT 0,
    duration_ms INT,
    error_message TEXT,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_csl_owner ON contact_sync_log(owner_id);
CREATE INDEX IF NOT EXISTS idx_csl_account ON contact_sync_log(account_id);
