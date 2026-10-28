CREATE TABLE IF NOT EXISTS strike_access (
    user_id      BIGINT PRIMARY KEY,
    purchased_at TIMESTAMPTZ DEFAULT now(),
    payment_ref  TEXT,
    granted_by   BIGINT  -- NULL = automatic, user_id = manual admin grant
);
