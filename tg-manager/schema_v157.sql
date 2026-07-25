-- schema_v157: разовая чистка сырых плейсхолдеров шаблонов у УЖЕ созданных ботов.
--
-- До фикса подстановки (bots.py::cb_pset_apply писал preset["template"] сырым)
-- у ботов, к которым применяли шаблон «Поддержка», в БД осели авто-ответы и шаги
-- воронок с дословными {{COMPANY}}/{{HOURS}}/{{OPERATOR_LINE}}. Фикс кода лечит
-- только НОВЫЕ применения; эта миграция подставляет те же значения по умолчанию
-- в существующие строки. Плейсхолдеры используются ТОЛЬКО пресетом support_bot,
-- значения — из его customize_fields (default_subs): COMPANY='нашу службу
-- поддержки', HOURS='пн-пт 9:00-18:00 МСК', OPERATOR_LINE='' (оператор не задан).
--
-- Идемпотентно: условие LIKE '%{{%}}%' перестаёт совпадать после первого прогона,
-- поэтому повторные применения при рестарте безопасны.

UPDATE auto_replies
SET response_text = REPLACE(REPLACE(REPLACE(
        response_text,
        '{{COMPANY}}', 'нашу службу поддержки'),
        '{{HOURS}}', 'пн-пт 9:00-18:00 МСК'),
        '{{OPERATOR_LINE}}', '')
WHERE response_text LIKE '%{{%}}%';

UPDATE funnel_steps
SET message_text = REPLACE(REPLACE(REPLACE(
        message_text,
        '{{COMPANY}}', 'нашу службу поддержки'),
        '{{HOURS}}', 'пн-пт 9:00-18:00 МСК'),
        '{{OPERATOR_LINE}}', '')
WHERE message_text LIKE '%{{%}}%';
