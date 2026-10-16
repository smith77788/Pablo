-- schema_v59: platform_settings — key-value store for global feature flags
CREATE TABLE IF NOT EXISTS platform_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO platform_settings (key, value)
VALUES ('free_mode', 'false')
ON CONFLICT (key) DO NOTHING;
