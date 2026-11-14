CREATE TABLE IF NOT EXISTS restriction_cleanup_receipts (
    operation_id BIGINT NOT NULL REFERENCES operation_queue(id) ON DELETE CASCADE,
    owner_id BIGINT NOT NULL,
    confirmation_id TEXT NOT NULL,
    request_scope JSONB NOT NULL,
    response JSONB NOT NULL,
    PRIMARY KEY (operation_id, owner_id, confirmation_id)
);
