-- schema_v75: Intent Engine — operation links for Memory Feedback Loop (Epoch IV)
CREATE TABLE IF NOT EXISTS intent_operation_links (
    intent_id  BIGINT NOT NULL,
    op_id      BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (intent_id, op_id)
);
CREATE INDEX IF NOT EXISTS idx_iol_op_id ON intent_operation_links(op_id);
