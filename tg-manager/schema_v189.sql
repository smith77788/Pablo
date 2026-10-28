-- Починка CHECK-констрейнта плана подписки.
-- Код перешёл на модель тарифов free/paid (bot/utils/tariffs.PLAN_LEVELS =
-- {free, paid}), а базовая схема оставила старый CHECK, разрешавший только
-- 'starter'/'pro'/'enterprise'. Из-за этого ЛЮБАЯ выдача плана 'paid' (мини-апп
-- «Выдать на N мес», billing.activate_subscription, восстановление подписок из
-- платежей) падала с CheckViolationError → «Внутренняя ошибка сервиса».
-- Разрешаем текущие тарифы (free, paid) и сохраняем старые имена для совместимости
-- с уже существующими строками.
ALTER TABLE subscriptions DROP CONSTRAINT IF EXISTS subscriptions_plan_check;
ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_plan_check
    CHECK (plan = ANY (ARRAY['free', 'paid', 'starter', 'pro', 'enterprise']));
