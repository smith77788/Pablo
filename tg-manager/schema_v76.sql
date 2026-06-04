-- schema_v76: Add missing template_id column to operation_queue
ALTER TABLE operation_queue ADD COLUMN IF NOT EXISTS template_id INT;