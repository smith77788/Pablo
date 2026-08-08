-- Notification preferences per user
CREATE TABLE IF NOT EXISTS notification_settings (
    user_id          BIGINT PRIMARY KEY,
    new_user         BOOLEAN NOT NULL DEFAULT TRUE,
    flood_warning    BOOLEAN NOT NULL DEFAULT TRUE,
    position_change  BOOLEAN NOT NULL DEFAULT TRUE,
    op_complete      BOOLEAN NOT NULL DEFAULT TRUE,
    restriction      BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Extend operation_queue with scheduled_for (for planned operations)
ALTER TABLE operation_queue
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_op_queue_scheduled
    ON operation_queue(scheduled_for)
    WHERE scheduled_for IS NOT NULL AND status = 'pending';
