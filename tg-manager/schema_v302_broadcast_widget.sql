-- schema_v302: рассылка HTML-виджета подписчикам бота.
--
-- widget_id — общий виджет (bot_widgets.widget_id), который рассылка шлёт
-- каждому получателю через sendSharedWidget вместо текста: у всех один счёт
-- голосов, правка доходит до всех копий. NULL — обычная рассылка текстом.
-- Пишется ТЕМ ЖЕ INSERT, что и сама рассылка: строка в 'pending' без признака
-- ушла бы при докатке аудитории текстом.
ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS widget_id TEXT;
