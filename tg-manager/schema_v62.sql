-- Добавляем поле prev_members для отслеживания динамики у конкурентов
ALTER TABLE competitors ADD COLUMN IF NOT EXISTS prev_members INT;
