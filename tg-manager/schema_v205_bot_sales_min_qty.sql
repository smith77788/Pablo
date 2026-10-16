-- Менеджер по продажам: минимальный заказ / порог доставки по товару.
-- Проблема: бот «тупил» — предлагал 1 шт/1 г, хотя по товару доставка от 2.
-- min_qty — минимально допустимое количество для заказа этого товара;
-- unit — единица измерения (шт, г, кг, мл, …) для человечных формулировок.
-- order_rules — свободный текст правил заказа/доставки на уровне персоны
-- (например: «доставка от 2 г по каждой позиции; самовывоз без ограничений»).

ALTER TABLE bot_sales_products
    ADD COLUMN IF NOT EXISTS min_qty INT NOT NULL DEFAULT 1;
ALTER TABLE bot_sales_products
    ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'шт';

ALTER TABLE bot_sales_personas
    ADD COLUMN IF NOT EXISTS order_rules TEXT NOT NULL DEFAULT '';

-- Оплата: бот не давал реквизиты и не переводил на оплату — заказ «повисал».
-- payment_details — реквизиты/инструкция оплаты (бот сообщает клиенту);
-- payment_via_operator — оплату принимает живой оператор (бот переводит на него).
ALTER TABLE bot_sales_personas
    ADD COLUMN IF NOT EXISTS payment_details TEXT NOT NULL DEFAULT '';
ALTER TABLE bot_sales_personas
    ADD COLUMN IF NOT EXISTS payment_via_operator BOOLEAN NOT NULL DEFAULT FALSE;

-- min_qty не может быть меньше 1 (0/отрицательное = отсутствие ограничения = 1).
UPDATE bot_sales_products SET min_qty = 1 WHERE min_qty IS NULL OR min_qty < 1;
