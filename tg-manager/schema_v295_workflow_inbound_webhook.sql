-- schema_v295: запуск сценария входящим вебхуком.
--
-- ЧТО БЫЛО. Сценарий запускался только изнутри: /start, слово, тег, оплата,
-- расписание. Сайт, CRM или форма не могли сказать «запусти дожим вот этому
-- человеку» — владелец делал это руками.
--
-- ЧТО ЗДЕСЬ. workflow_definitions.inbound_token — секрет в адресе
-- POST /webhook/scenario/{token}. Появляется, когда владелец выбирает запуск
-- «Входящий вебхук». Новый токен выдаётся по кнопке, старый сразу перестаёт
-- работать. NULL — сценарий снаружи не запускается.
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS inbound_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_definitions_inbound_token
    ON workflow_definitions (inbound_token) WHERE inbound_token IS NOT NULL;
