-- schema_v313: оплата подписки картой через провайдера Telegram Payments.
--
-- Счёт картой — строка payments с wallet_address='telegram_card' и валютой
-- провайдера (RUB, USD, EUR … — трёхбуквенный код ISO 4217): amount_crypto —
-- сумма в минимальных единицах (копейки/центы), tx_hash —
-- telegram_payment_charge_id. Ограничение валют пускало только TON, USDT_TRC20
-- и XTR, поэтому заменяем его целиком (снимаем и ставим в одной транзакции).
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_currency_check;
ALTER TABLE payments ADD CONSTRAINT payments_currency_check
    CHECK (currency IN ('TON', 'USDT_TRC20', 'XTR')
           OR (wallet_address = 'telegram_card' AND currency ~ '^[A-Z]{3}$'));

-- Один платёж картой — одно подтверждение: повторная доставка
-- successful_payment не продлевает подписку второй раз.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_card_charge
    ON payments(tx_hash) WHERE wallet_address = 'telegram_card' AND tx_hash IS NOT NULL;
