-- Schema v5: Message funnels (chains)

CREATE TABLE IF NOT EXISTS funnels (
    id SERIAL PRIMARY KEY,
    bot_id BIGINT NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    trigger_type TEXT NOT NULL CHECK (trigger_type IN ('start', 'keyword')),
    keyword TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS funnel_steps (
    id SERIAL PRIMARY KEY,
    funnel_id INTEGER NOT NULL REFERENCES funnels(id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    message_text TEXT NOT NULL,
    delay_minutes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(funnel_id, step_order)
);

CREATE TABLE IF NOT EXISTS funnel_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    funnel_id INTEGER NOT NULL REFERENCES funnels(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    next_send_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(funnel_id, user_id)
);
