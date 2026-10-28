-- v20: Fix bot_routing_weights.weight column type to support decimal values
ALTER TABLE bot_routing_weights ALTER COLUMN weight TYPE NUMERIC(10,2) USING weight::NUMERIC(10,2);
ALTER TABLE bot_routing_weights ALTER COLUMN weight SET DEFAULT 50.0;
