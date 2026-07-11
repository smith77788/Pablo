-- Auto-reoptimization of bot search SEO on ranking drop (POWER_USER_ROADMAP P2).
--
-- Ранжирование в Telegram-поиске для ботов идёт по токенам имени/описания. Когда
-- позиция бота по ключу падает ниже порога (visibility alert), система может
-- автоматически СГЕНЕРИРОВАТЬ рекомендацию по переоптимизации (имя/краткое
-- описание, где всплывает просевший ключ) и сохранить её для применения в один
-- клик. Применение — всегда инициируется оператором (setMyName/
-- setMyShortDescription по токену бота), без фонового авто-переименования.
--
-- Замыкает петлю анализ→рекомендация→применение для СТОРОНЫ БОТОВ (у каналов эта
-- петля уже есть: seo_ai_suggestions + /api/miniapp/seo/apply).

ALTER TABLE visibility_alert_settings
    ADD COLUMN IF NOT EXISTS auto_reoptimize BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS bot_seo_suggestions (
    owner_id    BIGINT NOT NULL,
    bot_id      BIGINT NOT NULL,
    name        TEXT,
    short_desc  TEXT,
    reason      TEXT,
    keyword     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_at  TIMESTAMPTZ,
    PRIMARY KEY (owner_id, bot_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_seo_sugg_owner
    ON bot_seo_suggestions(owner_id, created_at DESC);
