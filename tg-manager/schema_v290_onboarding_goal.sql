-- schema_v290: цель новичка в онбординге (services/onboarding.py, GOALS).
--
-- Лидеры первым вопросом спрашивают, зачем человек пришёл, и ведут каждого
-- своим путём. Здесь хранится ответ: продавец через бота, CRM или сбор
-- аудитории с рассылкой. Нет строки — цель не выбрана, чеклист идёт исходным
-- путём и экран сначала спрашивает цель. Сменить цель можно в любой момент:
-- строка одна на владельца.
CREATE TABLE IF NOT EXISTS onboarding_goals (
    owner_id  BIGINT PRIMARY KEY,
    goal      TEXT NOT NULL CHECK (goal IN ('sell_bot', 'crm', 'outreach')),
    chosen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
