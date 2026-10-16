-- schema_v169.sql — антибот-капча «Модератора чатов»
--
-- Новичок мьютится и получает кнопку «Я не бот». Пока не нажал — он в этой
-- таблице. Прошёл → строка удаляется; не прошёл до expires_at → фоновый
-- сметатель (services/chat_guard_runner) кикает/банит и чистит запись.
-- Персистентно, чтобы наказание догнало нарушителя даже после рестарта бота.

CREATE TABLE IF NOT EXISTS guard_captcha_pending (
    chat_id        BIGINT NOT NULL,
    user_id        BIGINT NOT NULL,
    captcha_msg_id BIGINT DEFAULT 0,     -- id сообщения с кнопкой (удалить при разрешении)
    expires_at     TIMESTAMPTZ NOT NULL, -- дедлайн прохождения
    action         TEXT NOT NULL DEFAULT 'kick',  -- kick|ban при провале
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_guard_captcha_expires
    ON guard_captcha_pending(expires_at);
