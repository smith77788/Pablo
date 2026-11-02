-- schema_v271: основная валюта владельца и курсы пересчёта (когорты и LTV).
--
-- Заказы ботов-продавцов приходят в валюте прайса (рубли, доллары, звёзды),
-- расход на кампании — в своей. Без пересчёта аналитика не могла сложить
-- выручку в одно число: LTV, «итого» и окупаемость пустели у всякого, кто
-- продаёт больше чем в одной валюте (services/owner_currency.py).
--
-- 1. owner_money_settings — валюта отчётов владельца и его ручные курсы
--    {"USD": 95.0} = «1 USD стоит 95 единиц основной валюты». Ручной курс
--    важнее курса ЦБ: владелец знает, по какому курсу реально выводит деньги.
CREATE TABLE IF NOT EXISTS owner_money_settings (
    owner_id      BIGINT PRIMARY KEY,
    currency      TEXT NOT NULL DEFAULT 'RUB',
    manual_rates  JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. fx_rates — курс ЦБ РФ (рублей за 1 единицу), общий для всех владельцев.
--    Обновляется не чаще раза в 12 часов при открытии отчёта; при сбое
--    источника остаётся последний сохранённый курс.
CREATE TABLE IF NOT EXISTS fx_rates (
    currency      TEXT PRIMARY KEY,
    rub_per_unit  NUMERIC NOT NULL CHECK (rub_per_unit > 0),
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
