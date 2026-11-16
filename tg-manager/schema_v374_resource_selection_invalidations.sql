-- Последнее изменение выбора хранится независимо от срока жизни журнала.
-- Один ключ на ресурс каталога или на его связь с конкретным проектом.
CREATE TABLE IF NOT EXISTS resource_selection_invalidations (
    owner_id BIGINT NOT NULL,
    scope TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (owner_id, scope)
);

CREATE OR REPLACE FUNCTION resource_selection_audit_scope(event_action TEXT, event_target TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE STRICT AS $$
    SELECT CASE
        WHEN event_action IN ('channel_restore', 'channel_reconnect')
             AND event_target ~ '^-?[0-9]{1,19}$'
            THEN 'channel:' || event_target
        WHEN event_action = 'restriction_cleanup_disable'
             AND event_target ~ '^(channel|group):-?[0-9]{1,19}$'
            THEN 'channel:' || split_part(event_target, ':', 2)
        WHEN event_action = 'restriction_cleanup_detach'
             AND event_target ~ '^channel:-?[0-9]{1,19} project:[0-9]{1,10}$'
            THEN split_part(event_target, ' ', 2) || ' ' || split_part(event_target, ' ', 1)
        WHEN event_action = 'project_node_detach'
             AND event_target ~ '^project:[0-9]{1,10} node:[0-9]{1,10} channel:-?[0-9]{1,19}$'
            THEN split_part(event_target, ' ', 1) || ' ' || split_part(event_target, ' ', 3)
    END
$$;

CREATE OR REPLACE FUNCTION capture_resource_selection_invalidation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    scope_key TEXT := resource_selection_audit_scope(NEW.action, NEW.target);
BEGIN
    IF scope_key IS NOT NULL THEN
        INSERT INTO resource_selection_invalidations (owner_id, scope, changed_at)
        VALUES (NEW.owner_id, scope_key, GREATEST(NEW.occurred_at, clock_timestamp()))
        ON CONFLICT (owner_id, scope) DO UPDATE
        SET changed_at = GREATEST(resource_selection_invalidations.changed_at, EXCLUDED.changed_at);
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_resource_selection_invalidation ON operation_audit;
CREATE TRIGGER trg_resource_selection_invalidation
    AFTER INSERT ON operation_audit
    FOR EACH ROW
    WHEN (NEW.result = 'success' AND NEW.action IN (
        'channel_restore', 'channel_reconnect', 'restriction_cleanup_disable',
        'restriction_cleanup_detach', 'project_node_detach'
    ))
    EXECUTE FUNCTION capture_resource_selection_invalidation();

INSERT INTO resource_selection_invalidations (owner_id, scope, changed_at)
SELECT owner_id, scope, MAX(changed_at)
FROM (
    SELECT owner_id, resource_selection_audit_scope(action, target) AS scope, occurred_at AS changed_at
    FROM operation_audit
    WHERE result = 'success' AND action IN (
        'channel_restore', 'channel_reconnect', 'restriction_cleanup_disable',
        'restriction_cleanup_detach', 'project_node_detach'
    )
    UNION ALL
    SELECT owner_id, 'channel:' || channel_id::text, removed_at
    FROM removed_channels
    WHERE reason = 'owner'
) AS observed
WHERE scope IS NOT NULL AND changed_at IS NOT NULL
GROUP BY owner_id, scope
ON CONFLICT (owner_id, scope) DO UPDATE
SET changed_at = GREATEST(resource_selection_invalidations.changed_at, EXCLUDED.changed_at);
