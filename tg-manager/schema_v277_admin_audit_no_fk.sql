-- schema_v277: журнал админ-действий не теряет записи из-за внешних ключей.
--
-- admin_audit_log (schema_v39) ссылался на platform_users и для admin_id, и для
-- target_user_id. Запись журнала fail-soft (database/db.log_admin_action), так
-- что админ, которого нет в platform_users (вошёл по ADMIN_IDS и ни разу не
-- нажимал /start), или действие над чужим ID молча не оставляли следа. Журнал
-- аудита обязан принимать любой ID: это след, а не связь.
ALTER TABLE admin_audit_log DROP CONSTRAINT IF EXISTS admin_audit_log_admin_id_fkey;
ALTER TABLE admin_audit_log DROP CONSTRAINT IF EXISTS admin_audit_log_target_user_id_fkey;
-- Источник записи: бот, мини-апп или вход по секретной фразе.
ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'app';
