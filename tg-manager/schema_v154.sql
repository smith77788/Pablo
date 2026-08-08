-- v154: восстановление потерянной миграции + фикс уникального индекса CF-пула.
--
-- Контекст: два агента одновременно создали schema_v153.sql (один — CF relay,
-- другой — bot_seo_suggestions). Мердж «resolve schema_v153 conflict» оставил
-- только CF-версию → миграция авто-реоптимизации ботов ПОТЕРЯЛАСЬ, хотя код
-- (services/bot_reoptimizer.py, mini_app_api) её использует → рантайм-ошибка
-- «relation bot_seo_suggestions does not exist». Возвращаем здесь.

-- 1) Авто-реоптимизация SEO ботов (bot_reoptimizer): рекомендация имя/описание,
--    применяется оператором в один клик. ON CONFLICT(owner_id, bot_id) в коде →
--    нужен UNIQUE(owner_id, bot_id) — обеспечивается PRIMARY KEY.
CREATE TABLE IF NOT EXISTS bot_seo_suggestions (
    owner_id    BIGINT NOT NULL,
    bot_id      BIGINT NOT NULL,
    name        TEXT,
    short_desc  TEXT,
    reason      TEXT,
    keyword     TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    applied_at  TIMESTAMPTZ,
    PRIMARY KEY (owner_id, bot_id)
);

-- opt-in тумблер «авто-реоптимизация при падении позиции» (DEFAULT FALSE —
-- переименование бота полудеструктивно, включается осознанно).
ALTER TABLE visibility_alert_settings
    ADD COLUMN IF NOT EXISTS auto_reoptimize BOOLEAN DEFAULT FALSE;

-- 2) CF relay (перенос из schema_v153): имя файла v153 было занято двумя агентами
--    → раннер миграций пропускал CF-версию (тот же basename уже 'ok') → колонка
--    cf_relay_url и таблица cf_worker_pool НЕ создавались в БД, где v153 применялся
--    раньше в другой редакции. get_account_for_telethon/_ACCOUNT_COLS селектят
--    a.cf_relay_url → падал весь путь загрузки аккаунта (синк контактов и др.).
--    Повторяем идемпотентно здесь (новый файл v154 гарантированно применяется).
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS cf_relay_url TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_tg_accounts_cf_relay
    ON tg_accounts(cf_relay_url) WHERE cf_relay_url IS NOT NULL;

CREATE TABLE IF NOT EXISTS cf_worker_pool (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    worker_url TEXT NOT NULL,
    region TEXT DEFAULT 'auto',
    status TEXT DEFAULT 'active',
    assigned_accounts INTEGER DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cf_worker_pool_owner ON cf_worker_pool(owner_id);

-- 3) CF Worker pool: assign_urls_to_accounts делает INSERT ... ON CONFLICT
--    (owner_id, worker_url), но уникального индекса не было → INSERT падал
--    («no unique or exclusion constraint matching the ON CONFLICT specification»).
CREATE UNIQUE INDEX IF NOT EXISTS uq_cf_worker_pool_owner_url
    ON cf_worker_pool(owner_id, worker_url);
