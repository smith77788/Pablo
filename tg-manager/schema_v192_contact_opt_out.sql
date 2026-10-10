-- schema_v192_contact_opt_out.sql — глобальный реестр «не приглашать».
-- Первопричина: invite_target_log (op_worker.py) дедупит инвайты ТОЛЬКО в
-- пределах одной группы и не различает исход (joined/failed/left) — человек,
-- явно попросивший больше не приглашать, снова получит инвайт в СЛЕДУЮЩУЮ
-- группу. Ни unified_contacts (CRM), ни invite-движок не знают о таком отказе.
-- Эта таблица — заведомо ручной (оператор отмечает сам), но подключённый по
-- всем точкам: дедуп инвайта (глобально, не по группе) + карточка контакта.
CREATE TABLE IF NOT EXISTS contact_opt_out (
    owner_id   BIGINT NOT NULL,
    target     TEXT NOT NULL,       -- нормализованная форма: @username / id / +phone
    reason     TEXT,
    source     TEXT NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, target)
);
