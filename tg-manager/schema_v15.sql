-- v15: Telegram account manager + search ranking tracker

CREATE TABLE IF NOT EXISTS tg_accounts (
    id          SERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    phone       TEXT NOT NULL,
    session_str TEXT NOT NULL,
    tg_user_id  BIGINT,
    first_name  TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    added_at    TIMESTAMPTZ DEFAULT now(),
    last_used   TIMESTAMPTZ,
    is_active   BOOLEAN DEFAULT true
);
CREATE INDEX IF NOT EXISTS idx_tg_accounts_owner ON tg_accounts(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_accounts_phone_owner ON tg_accounts(owner_id, phone);

CREATE TABLE IF NOT EXISTS tracked_keywords (
    id          SERIAL PRIMARY KEY,
    bot_id      BIGINT NOT NULL,
    owner_id    BIGINT NOT NULL,
    keyword     TEXT NOT NULL,
    is_active   BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(bot_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_keywords_bot ON tracked_keywords(bot_id);
CREATE INDEX IF NOT EXISTS idx_keywords_owner ON tracked_keywords(owner_id);

CREATE TABLE IF NOT EXISTS search_rankings (
    id          SERIAL PRIMARY KEY,
    keyword_id  INTEGER NOT NULL REFERENCES tracked_keywords(id) ON DELETE CASCADE,
    bot_id      BIGINT NOT NULL,
    position    INTEGER,
    checked_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_rankings_keyword ON search_rankings(keyword_id, checked_at DESC);
