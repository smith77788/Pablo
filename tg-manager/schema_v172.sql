-- Сегментация контактов по полу: колонка на unified_contacts (значения 'm'/'f',
-- NULL = не определён). Заполняется gender_classifier по имени/фамилии (при синке
-- и бэкофиллом). Индекс — для фильтра сегмента по полу.
ALTER TABLE unified_contacts
    ADD COLUMN IF NOT EXISTS gender TEXT;
CREATE INDEX IF NOT EXISTS idx_unified_contacts_gender
    ON unified_contacts(owner_id, gender);
