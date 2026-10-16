-- schema_v72: Broadcast delivery log for resumable/idempotent broadcasts

CREATE TABLE IF NOT EXISTS broadcast_delivery_log (
    broadcast_id  BIGINT NOT NULL,
    user_id       BIGINT NOT NULL,
    delivered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (broadcast_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_bdl_broadcast_id ON broadcast_delivery_log (broadcast_id);
