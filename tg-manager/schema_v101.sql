-- v101: Self-promo system (самопиар платформы BotMother)

CREATE TABLE IF NOT EXISTS self_promo_templates (
    id          SERIAL PRIMARY KEY,
    style       TEXT NOT NULL DEFAULT 'direct',
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    cta_text    TEXT,
    cta_url     TEXT,
    add_referral BOOLEAN DEFAULT TRUE,
    is_active   BOOLEAN DEFAULT TRUE,
    use_count   INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS self_promo_runs (
    id           BIGSERIAL PRIMARY KEY,
    template_id  INT REFERENCES self_promo_templates(id) ON DELETE SET NULL,
    run_type     TEXT NOT NULL DEFAULT 'channel_post',
    initiated_by BIGINT NOT NULL,
    sent         INT DEFAULT 0,
    failed       INT DEFAULT 0,
    status       TEXT DEFAULT 'running',
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS self_promo_templates_active_idx ON self_promo_templates(is_active);
CREATE INDEX IF NOT EXISTS self_promo_runs_user_idx        ON self_promo_runs(initiated_by, created_at DESC);

INSERT INTO self_promo_templates (style, title, content, cta_text, cta_url, add_referral)
SELECT style, title, content, cta_text, cta_url, add_referral FROM (VALUES
    ('direct'::text, '🚀 BotMother — прямая реклама',
     E'🚀 <b>BotMother</b> — Telegram-автоматизация нового уровня\n\n✅ Управление 100+ каналами из одного места\n✅ Массовые рассылки, прогрев аккаунтов, DM-кампании\n✅ Strike-зачистка, SEO, аналитика\n✅ Работает 24/7 без вашего участия\n\nЗапустите бесплатно →',
     '🤖 Запустить BotMother', 'https://t.me/BotMotherBot', TRUE),

    ('direct'::text, '💡 BotMother — экономия времени',
     E'💡 Сколько часов вы тратите на рутину в Telegram?\n\nПубликации, ответы, аналитика, прогрев — всё это можно автоматизировать.\n\n<b>BotMother</b> делает это за вас:\n• Авто-публикации по расписанию\n• Умная DM-рассылка по аудитории\n• Прогрев аккаунтов в автопилоте\n• Полная аналитика каналов\n\nПопробуйте →',
     '⚡ Попробовать бесплатно', 'https://t.me/BotMotherBot', TRUE),

    ('native'::text, '📊 Нативный: рост Telegram-канала',
     E'📊 <b>Как увеличить охваты в Telegram в 3 раза</b>\n\nТри вещи, которые реально работают в 2025:\n\n1️⃣ <b>Регулярность</b> — публикации каждый день в одно время\n2️⃣ <b>Кросс-постинг</b> — один контент, несколько каналов\n3️⃣ <b>Аналитика позиций</b> — следите за местом в поиске\n\nВсе три можно автоматизировать. Мы сами используем для этого специальный инструмент — делимся по запросу 👇',
     '🔧 Узнать инструмент', 'https://t.me/BotMotherBot', FALSE),

    ('native'::text, '🛠 Нативный: инструменты автоматизации',
     E'🛠 <b>Топ инструментов для Telegram-маркетолога в 2025</b>\n\nПроверено на практике:\n\n• <b>Планировщик постов</b> — публикуй заранее, не отвлекайся\n• <b>Менеджер аккаунтов</b> — держи все сессии под контролем\n• <b>DM-кампании</b> — персональные сообщения без бана\n• <b>Аналитика каналов</b> — знай своих конкурентов\n\nВсё это — в одном месте. Ссылка в комментарии 👇',
     '👇 Один инструмент', 'https://t.me/BotMotherBot', FALSE)
) AS t(style, title, content, cta_text, cta_url, add_referral)
WHERE NOT EXISTS (SELECT 1 FROM self_promo_templates);
