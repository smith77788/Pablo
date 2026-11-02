-- Три тарифа подписки по объёму флота (services/tiers.py): start / pro / business.
-- Уровень доступа к функциям остаётся plan='paid'; тариф хранится рядом.
-- NULL у действующей подписки — купленная до тарифов, триал, награда или
-- ручная выдача: лимита флота нет.
ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS tier TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS tier TEXT;

-- Автопродление подписки звёздами Telegram (Stars subscription, 30 дней).
-- Первый платёж — строка с wallet_address='telegram_stars_sub'; каждое
-- продление — своя подтверждённая строка с recurring_of = reference первого.
-- recurring_canceled_at — владелец отключил автопродление (editUserStarSubscription).
ALTER TABLE payments ADD COLUMN IF NOT EXISTS recurring_of TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS recurring_canceled_at TIMESTAMPTZ;
