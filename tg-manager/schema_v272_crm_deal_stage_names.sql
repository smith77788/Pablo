-- schema_v272: сделки на старых именах стадий — в новые.
--
-- Бот создавал и двигал сделки по набору new/contacted/qualified из
-- schema_v90, а crm_deals_stage_check (services/mini_app_api.py) разрешает
-- только lead/contact/proposal/negotiation/won/lost. Там, где проверка уже
-- стоит, таких строк нет, и запрос ничего не меняет; там, где её добавить не
-- удалось как раз из-за таких строк, он переводит их, и проверка встаёт.
UPDATE crm_deals
   SET stage = CASE stage WHEN 'new' THEN 'lead'
                          WHEN 'contacted' THEN 'contact'
                          WHEN 'qualified' THEN 'proposal' END
 WHERE stage IN ('new', 'contacted', 'qualified');
ALTER TABLE crm_deals ALTER COLUMN stage SET DEFAULT 'lead';
