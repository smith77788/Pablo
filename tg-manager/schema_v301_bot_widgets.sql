-- schema_v301: HTML-виджеты в чатах (Telegram HTMLBubbles, Bot API 10.4).
--
-- Виджет — сообщение бота с маленькой HTML-страницей внутри пузыря. Код
-- виджета после отправки не меняется никогда; меняются только его данные:
-- public_data (видят все, в том числе пересланные копии) и chat_data (только
-- в этом чате). Менять их может только бот — editMessageWidgetData по чату и
-- message_id или editSharedWidgetData по widget_id общего виджета. Поэтому
-- каждый отправленный виджет запоминаем: без строки здесь владелец не смог бы
-- поправить карточку или отсчёт после отправки, а бот — пересчитать голоса.
--
--   template      — шаблон (countdown | card | quiz | poll | …) или html — свой код;
--   title         — подпись для списка в мини-аппе;
--   widget_id     — id общего виджета (is_shared): по нему приходят касания
--                   из пересланных копий и правятся данные всех копий сразу;
--   public_data   — данные виджета как отправлены / последний раз изменены;
--   allow_queries — виджет может спрашивать бота (опрос).
CREATE TABLE IF NOT EXISTS bot_widgets (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT      NOT NULL,
    bot_id         BIGINT      NOT NULL REFERENCES managed_bots(bot_id) ON DELETE CASCADE,
    chat_id        BIGINT      NOT NULL,
    message_id     BIGINT      NOT NULL,
    widget_id      TEXT,
    template       TEXT        NOT NULL DEFAULT 'html',
    title          TEXT        NOT NULL DEFAULT '',
    public_data    TEXT        NOT NULL DEFAULT '',
    chat_data      TEXT        NOT NULL DEFAULT '',
    allow_queries  BOOLEAN     NOT NULL DEFAULT FALSE,
    revision       INT         NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (bot_id, chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_bot_widgets_owner_created
    ON bot_widgets(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_widgets_bot_widget
    ON bot_widgets(bot_id, widget_id) WHERE widget_id IS NOT NULL;

-- Голоса в виджетах-голосованиях (опрос, оценка, «кто идёт»): один голос
-- человека на виджет; повторное касание того же варианта голос снимает.
-- voter_name — имя из Telegram, для списка «Идут: …».
CREATE TABLE IF NOT EXISTS bot_widget_votes (
    widget_row  BIGINT      NOT NULL REFERENCES bot_widgets(id) ON DELETE CASCADE,
    voter_id    BIGINT      NOT NULL,
    voter_name  TEXT        NOT NULL DEFAULT '',
    option      SMALLINT    NOT NULL CHECK (option BETWEEN 0 AND 9),
    voted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (widget_row, voter_id)
);
