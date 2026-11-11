-- schema_v321: «Клон и адаптация» — история с подробностями.
--
-- Было: одна строка на пару «источник → цель» со списком полей через запятую
-- и строкой «OK (3 полей)». По ней не понять, ЧТО стало у цели, какие поля
-- уже совпадали, какой операцией это сделано и можно ли вернуть как было.
--
-- op_id     — операция, сделавшая клон: по ней экран находит снимок отката
--             (bulk_edit_snapshots.op_id) и журнал операции;
-- changes   — по каждому полю: подпись, было, стало, итог (ok/same/error)
--             и причина ошибки — по-русски, готовое для экрана;
-- settings  — настройки адаптации прогона (шаблон имени, замены, языки):
--             «Повторить» на экране берёт их отсюда.
-- status теперь ещё 'partial' (часть полей не применилась) и 'same' (у цели
-- всё уже было таким же — Telegram не трогали).
ALTER TABLE clone_adapt_history ADD COLUMN IF NOT EXISTS op_id BIGINT;
ALTER TABLE clone_adapt_history ADD COLUMN IF NOT EXISTS changes JSONB;
ALTER TABLE clone_adapt_history ADD COLUMN IF NOT EXISTS settings JSONB;
CREATE INDEX IF NOT EXISTS clone_adapt_history_owner_op_idx
    ON clone_adapt_history(owner_id, op_id);
