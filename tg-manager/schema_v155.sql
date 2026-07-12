-- v155: broadcasts.silent — недостающая колонка «тихой» рассылки.
--
-- Код ПИШЕТ и ЧИТАЕТ broadcasts.silent, но колонки в схеме не было:
--   * запись (db.create_broadcast, mini_app_api INSERT) — с fallback на
--     UndefinedColumnError → значение silent молча терялось (тихий режим не
--     сохранялся, хотя broadcaster его потребляет: disable_notification=silent);
--   * ЧТЕНИЕ без fallback: broadcaster.get_broadcast_analytics делает
--     `SELECT b.buttons, b.silent …` → на отсутствующей колонке падало
--     UndefinedColumnError → эндпоинт аналитики рассылки (/api/miniapp/
--     broadcast/{id}/analytics) всегда возвращал 500.
--
-- Добавление колонки: (1) чинит эндпоинт аналитики; (2) замыкает фичу «тихая
-- рассылка» (значение теперь переживает запись → broadcaster.py шлёт
-- disable_notification=True). DEFAULT FALSE = текущее фактическое поведение,
-- поэтому изменение обратно совместимо и безопасно.
ALTER TABLE broadcasts
    ADD COLUMN IF NOT EXISTS silent BOOLEAN NOT NULL DEFAULT FALSE;
