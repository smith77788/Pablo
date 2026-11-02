-- schema_v281: что владелец делает с подсказками «Что делать дальше».
--
-- Копайлот не помнил реакции: «отложить» жило в localStorage одного
-- устройства, а принятые подсказки не поднимались выше. Здесь каждое
-- «открыл», «применил», «отложил» — строка; services/next_actions поднимает
-- то, чем владелец пользуется, опускает то, что он откладывает, и прячет
-- отложенное на сутки на всех устройствах.
CREATE TABLE IF NOT EXISTS copilot_feedback (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    action_id   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('opened', 'applied', 'snoozed')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS copilot_feedback_owner_idx
    ON copilot_feedback (owner_id, created_at DESC);
