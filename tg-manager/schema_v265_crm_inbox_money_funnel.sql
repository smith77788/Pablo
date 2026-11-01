-- schema_v265: единый инбокс ответов и расходы кампаний для воронки денег.
--
-- 1. inbox_messages — входящие личные сообщения на аккаунты флота.
--    Слушатель (services/audience_listener.py) получал их и отдавал только
--    сенсору намерений: текст нигде не сохранялся, и прочитать, что человек
--    ответил на рассылку, можно было лишь в самом Telegram, открыв нужный
--    аккаунт. Входящие бизнес-аккаунтов уже лежат в vault_messages, диалоги
--    ботов-продавцов — в bot_sales_dialogs; эта таблица закрывает третий
--    источник, и инбокс (services/unified_inbox.py) сводит все три.
--    Текст шифруется так же, как в Хранилище (token_vault, ENC:...).
CREATE TABLE IF NOT EXISTS inbox_messages (
    id             BIGSERIAL PRIMARY KEY,
    owner_id       BIGINT NOT NULL,
    account_id     BIGINT NOT NULL,             -- какой аккаунт флота получил
    peer_user_id   BIGINT NOT NULL,             -- кто написал
    peer_name      TEXT,
    peer_username  TEXT,
    msg_id         BIGINT NOT NULL,             -- id сообщения в чате аккаунта
    text_enc       TEXT,
    msg_date       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_read        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, peer_user_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_owner_date
    ON inbox_messages (owner_id, msg_date DESC);
CREATE INDEX IF NOT EXISTS idx_inbox_messages_owner_peer
    ON inbox_messages (owner_id, peer_user_id, msg_date DESC);

-- 2. campaign_spend — сколько владелец потратил на кампанию. Без этого числа
--    «стоимость лида» и окупаемость не посчитать: система знает охват и
--    покупки, но не знает цену аккаунтов, прокси и рекламы. Вводится руками
--    на экране воронки денег, одна строка на кампанию.
CREATE TABLE IF NOT EXISTS campaign_spend (
    owner_id       BIGINT NOT NULL,
    campaign_kind  TEXT   NOT NULL DEFAULT 'dm',   -- dm (dm_campaigns)
    campaign_id    BIGINT NOT NULL,
    amount         NUMERIC(14,2) NOT NULL DEFAULT 0,
    currency       TEXT   NOT NULL DEFAULT 'RUB',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, campaign_kind, campaign_id)
);

-- Поиск получателей кампании по человеку: карточка клиента спрашивает
-- «что ему отправляли», воронка — «кто из получателей ответил/купил».
CREATE INDEX IF NOT EXISTS idx_dm_campaign_log_user
    ON dm_campaign_log (tg_user_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_bot_sales_orders_customer
    ON bot_sales_orders (owner_id, customer_chat_id);
