-- v102: Growth Engine — амбассадор-программа, комиссии, контент-сидер, выплаты

-- Тиры амбассадоров (фиксированные определения)
CREATE TABLE IF NOT EXISTS ambassador_tiers (
    tier_key       TEXT PRIMARY KEY,
    sort_order     INT NOT NULL,
    tier_name      TEXT NOT NULL,
    tier_emoji     TEXT NOT NULL,
    min_active_refs INT NOT NULL DEFAULT 0,
    min_paid_refs  INT NOT NULL DEFAULT 0,
    reward_days    INT DEFAULT 0,
    reward_plan    TEXT DEFAULT 'starter',
    commission_pct NUMERIC(5,2) DEFAULT 0,
    badge_label    TEXT
);

-- Статус амбассадора каждого пользователя
CREATE TABLE IF NOT EXISTS ambassador_status (
    user_id          BIGINT PRIMARY KEY,
    tier_key         TEXT REFERENCES ambassador_tiers(tier_key) DEFAULT NULL,
    total_commission NUMERIC(12,4) DEFAULT 0,
    paid_commission  NUMERIC(12,4) DEFAULT 0,
    last_tier_up_at  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Начисления комиссии (по каждому платежу реферала)
CREATE TABLE IF NOT EXISTS commission_ledger (
    id             BIGSERIAL PRIMARY KEY,
    referrer_id    BIGINT NOT NULL,
    referred_id    BIGINT NOT NULL,
    payment_amount NUMERIC(10,4) NOT NULL,  -- сумма платежа реферала (USD)
    commission_pct NUMERIC(5,2) NOT NULL,
    commission_usd NUMERIC(10,4) NOT NULL,
    status         TEXT DEFAULT 'pending',   -- 'pending' | 'paid' | 'cancelled'
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    paid_at        TIMESTAMPTZ
);

-- Запросы на выплату комиссии
CREATE TABLE IF NOT EXISTS payout_requests (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    amount_usd   NUMERIC(10,4) NOT NULL,
    method       TEXT DEFAULT 'usdt_trc20',
    wallet       TEXT,
    status       TEXT DEFAULT 'pending',  -- 'pending' | 'approved' | 'paid' | 'rejected'
    admin_note   TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

-- Контент-пакеты (шаблоны с плейсхолдерами для реальной статистики)
CREATE TABLE IF NOT EXISTS growth_content_seeds (
    id             SERIAL PRIMARY KEY,
    content_type   TEXT NOT NULL,  -- 'stats' | 'native' | 'direct' | 'case'
    title          TEXT NOT NULL,
    template       TEXT NOT NULL,  -- плейсхолдеры: {users} {ops} {channels} {ref_link}
    deployed_count INT DEFAULT 0,
    is_active      BOOLEAN DEFAULT TRUE,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS commission_ledger_referrer_idx ON commission_ledger(referrer_id, status);
CREATE INDEX IF NOT EXISTS payout_requests_user_idx ON payout_requests(user_id, status);

-- Seed: тиры амбассадоров
INSERT INTO ambassador_tiers (tier_key, sort_order, tier_name, tier_emoji, min_active_refs, min_paid_refs, reward_days, reward_plan, commission_pct, badge_label)
SELECT * FROM (VALUES
    ('explorer', 1, 'Исследователь', '🌱', 1,   0,   7,   'starter',    0,    NULL),
    ('starter',  2, 'Стартер',       '🥉', 3,   0,   30,  'starter',    0,    NULL),
    ('silver',   3, 'Серебро',       '🥈', 0,   5,   90,  'starter',    5,    NULL),
    ('gold',     4, 'Золото',        '🥇', 0,   15,  180, 'pro',        10,   NULL),
    ('elite',    5, 'Элита',         '💎', 0,   50,  365, 'enterprise', 20,   NULL),
    ('legend',   6, 'Легенда',       '👑', 0,   200, 0,   'enterprise', 30,   '👑 Легенда BotMother')
) AS t(tier_key, sort_order, tier_name, tier_emoji, min_active_refs, min_paid_refs, reward_days, reward_plan, commission_pct, badge_label)
WHERE NOT EXISTS (SELECT 1 FROM ambassador_tiers);

-- Seed: контент-пакеты
INSERT INTO growth_content_seeds (content_type, title, template)
SELECT content_type, title, template FROM (VALUES
    ('stats', '📊 Статистика платформы',
     E'📊 <b>BotMother в цифрах</b>\n\n'
     E'👥 {users}+ пользователей доверяют нам управление Telegram\n'
     E'⚡ {ops}+ операций выполнено автоматически\n'
     E'📡 {channels}+ каналов под управлением\n\n'
     E'Telegram-автоматизация нового поколения. Попробуй бесплатно:\n'
     E'{ref_link}'),

    ('native', '💡 Нативный совет',
     E'💡 <b>5 вещей, которые тормозят рост вашего Telegram-канала</b>\n\n'
     E'1️⃣ Ручные публикации — тратите часы на то, что можно автоматизировать\n'
     E'2️⃣ Нет кросс-постинга — один контент = один канал (грустно)\n'
     E'3️⃣ Не отслеживаете позиции в поиске — конкуренты обходят, а вы не знаете\n'
     E'4️⃣ Прогрев аккаунтов вручную — риск бана без нужды\n'
     E'5️⃣ Нет системы DM-рассылок — тысячи потенциальных подписчиков игнорируются\n\n'
     E'Всё это решается в одном месте 👉 {ref_link}'),

    ('direct', '🚀 Прямая реклама',
     E'🚀 <b>BotMother</b> — если вы серьёзно занимаетесь Telegram\n\n'
     E'✅ Управляй сотнями каналов из одного интерфейса\n'
     E'✅ Автоматические рассылки, DM-кампании, прогрев\n'
     E'✅ Strike-зачистка конкурентов, SEO-аудит, аналитика\n'
     E'✅ Работает 24/7 без вашего участия\n\n'
     E'Сейчас {users}+ команд уже автоматизировали свой Telegram.\n'
     E'Ваша очередь → {ref_link}'),

    ('case', '📈 Кейс-стади',
     E'📈 <b>Как автоматизация Telegram изменила мой подход к контент-маркетингу</b>\n\n'
     E'Раньше: 3 часа в день на ручные публикации, репосты и ответы.\n'
     E'Теперь: 15 минут настройки — и всё работает само.\n\n'
     E'Что изменилось:\n'
     E'→ Публикации идут по расписанию в 8 каналов одновременно\n'
     E'→ DM-кампании охватывают целевую аудиторию без спама\n'
     E'→ Прогрев аккаунтов не требует ручного труда\n'
     E'→ Позиции в поиске отслеживаются автоматически\n\n'
     E'Инструмент: BotMother. Попробуй сам → {ref_link}')
) AS t(content_type, title, template)
WHERE NOT EXISTS (SELECT 1 FROM growth_content_seeds);
