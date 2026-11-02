-- schema_v285: подключённые боты на вебхуке и возврат на опрос.
--
-- managed_bots.use_webhook (schema_v225) теперь значит «обновления приходят
-- вебхуком»: опрос таких ботов не трогает (services/managed_bot_webhooks.py).
-- webhook_failed_at — когда Telegram в последний раз не смог доставить
-- вебхук этому боту: бот возвращён на опрос и не переводится обратно
-- несколько часов.

ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS use_webhook BOOLEAN DEFAULT FALSE;
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS webhook_failed_at TIMESTAMPTZ;
