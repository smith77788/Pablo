-- schema_v264: оплата подписки Telegram Stars.
--
-- Счёт в Stars — обычная строка payments с currency='XTR' (код валюты Stars в
-- Bot API): amount_crypto — число звёзд, tx_hash — telegram_payment_charge_id.
-- Ограничение валют было зашито в CREATE TABLE (v13) и пускало только TON и
-- USDT_TRC20, поэтому заменяем его целиком. Ограничение снимается и ставится в
-- одном файле, а файл выполняется транзакцией.
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_currency_check;
ALTER TABLE payments ADD CONSTRAINT payments_currency_check
    CHECK (currency IN ('TON', 'USDT_TRC20', 'XTR'));

-- Один платёж Stars — одно подтверждение: повторная доставка successful_payment
-- не продлевает подписку второй раз.
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_xtr_charge
    ON payments(tx_hash) WHERE currency = 'XTR' AND tx_hash IS NOT NULL;
