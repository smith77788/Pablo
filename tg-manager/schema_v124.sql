-- schema_v124: Anti-abuse — одинаковые телефоны у разных платформ-пользователей
-- Защита от создания нескольких аккаунтов для обхода бесплатного тарифа.

-- Маппинг телефон → владелец (автоматически заполняется при добавлении tg_accounts)
CREATE TABLE IF NOT EXISTS phone_owner_links (
    phone       TEXT    NOT NULL,
    owner_id    BIGINT  NOT NULL REFERENCES platform_users(user_id) ON DELETE CASCADE,
    linked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (phone, owner_id)
);

CREATE INDEX IF NOT EXISTS idx_phone_owner_phone ON phone_owner_links(phone);
CREATE INDEX IF NOT EXISTS idx_phone_owner_owner ON phone_owner_links(owner_id);

-- Связь между платформ-пользователями (разделяют телефонные номера)
CREATE TABLE IF NOT EXISTS linked_platform_users (
    owner_id_a  BIGINT NOT NULL,
    owner_id_b  BIGINT NOT NULL,
    link_type   TEXT   NOT NULL DEFAULT 'shared_phone',
    linked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id_a, owner_id_b),
    CHECK (owner_id_a < owner_id_b)
);

CREATE INDEX IF NOT EXISTS idx_linked_users_a ON linked_platform_users(owner_id_a);
CREATE INDEX IF NOT EXISTS idx_linked_users_b ON linked_platform_users(owner_id_b);

-- Бэкфилл: перенести существующие телефоны из tg_accounts в phone_owner_links
INSERT INTO phone_owner_links (phone, owner_id, linked_at)
SELECT DISTINCT phone, owner_id, COALESCE(added_at, NOW())
FROM tg_accounts
WHERE phone IS NOT NULL AND phone != ''
ON CONFLICT DO NOTHING;

-- Автоопределение уже существующих связей по общим телефонам
INSERT INTO linked_platform_users (owner_id_a, owner_id_b, link_type, linked_at)
SELECT DISTINCT
    LEAST(a.owner_id, b.owner_id)    AS owner_id_a,
    GREATEST(a.owner_id, b.owner_id) AS owner_id_b,
    'shared_phone',
    NOW()
FROM phone_owner_links a
JOIN phone_owner_links b ON a.phone = b.phone AND a.owner_id != b.owner_id
ON CONFLICT DO NOTHING;
