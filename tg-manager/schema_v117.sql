-- schema_v117: Persona Ecosystem — AI personas with persistent memory

CREATE TABLE IF NOT EXISTS persona_profiles (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT UNIQUE,
    owner_id BIGINT NOT NULL,
    persona_name VARCHAR(64) NOT NULL,
    bio TEXT,
    age INT DEFAULT 25,
    interests TEXT[] DEFAULT ARRAY[]::TEXT[],
    speech_style VARCHAR(32) DEFAULT 'neutral',  -- formal/casual/expert/friendly/sarcastic
    tone VARCHAR(32) DEFAULT 'positive',
    niche VARCHAR(64),
    backstory TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS persona_memory (
    id BIGSERIAL PRIMARY KEY,
    persona_id BIGINT REFERENCES persona_profiles(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    event_type VARCHAR(32),  -- 'comment', 'reaction', 'follow', 'message', 'post'
    content TEXT,
    entity VARCHAR(255),  -- channel/group/user it interacted with
    sentiment VARCHAR(16),  -- positive/neutral/negative
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_persona_profiles_owner ON persona_profiles(owner_id) WHERE is_active=TRUE;
CREATE INDEX IF NOT EXISTS idx_persona_memory_persona_time ON persona_memory(persona_id, created_at DESC);
