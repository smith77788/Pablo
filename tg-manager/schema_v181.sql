-- Сессионный доступ в админку — в БД вместо памяти процесса.
--
-- ЗАЧЕМ (находка аудита №3). Вход по секретной фразе складывался в set уровня
-- модуля (bot.handlers.admin._session_admins), а от него зависит РЕШЕНИЕ О
-- ДОСТУПЕ (_is_admin в боте, мини-аппе и проверке тарифа). Следствия:
--   • при нескольких процессах (ROLE=web/worker, реплики) доступ зависел от
--     того, какой процесс обслужил запрос: вошёл в боте — в мини-аппе не админ;
--   • доступ не истекал ВООБЩЕ — жил до перезапуска процесса, то есть неделями;
--   • перезапуск молча выкидывал всех вошедших.
--
-- Теперь сессия — строка с сроком годности, общая для всех процессов.

CREATE TABLE IF NOT EXISTS platform_admin_sessions (
    user_id     BIGINT PRIMARY KEY,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

-- Горячий запрос ровно один: «все живые сессии» на обновление кэша процесса.
CREATE INDEX IF NOT EXISTS idx_platform_admin_sessions_alive
    ON platform_admin_sessions (expires_at);
