-- v49: Add crm_contacts table for DM campaigns CRM-targeting
CREATE TABLE IF NOT EXISTS crm_contacts (
    id           SERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    tg_user_id   BIGINT NOT NULL,
    username     TEXT,
    first_name   TEXT,
    last_name    TEXT,
    phone        TEXT,
    tags         TEXT[] DEFAULT '{}',
    notes        TEXT,
    source       TEXT,          -- how the contact was acquired (parsed, imported, manual)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_contacts_owner_user
    ON crm_contacts(owner_id, tg_user_id);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_owner
    ON crm_contacts(owner_id);
