-- schema_v320: правила модели поведения, которые настраивает владелец.
--
-- До этого все правила слоя (какую ступень даёт «спросил цену», сколько
-- часов живёт «готов купить», сколько горячих делают аудиторию горячей) были
-- зашиты в код. Одна строка на владельца; содержимое проверяет
-- virtual_layer.normalize_rules, отсутствие строки = стандартные правила.
CREATE TABLE IF NOT EXISTS vlayer_rules (
    owner_id    BIGINT PRIMARY KEY,
    rules       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
