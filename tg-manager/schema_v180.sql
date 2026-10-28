-- Аренда аккаунта под живую сессию: межпроцессный арбитр вместо памяти процесса.
--
-- ЗАЧЕМ. Инвариант «одна auth-key сессия НИКОГДА не коннектится из двух мест»
-- (нарушение = AUTH_KEY_DUPLICATED, сессия умирает безвозвратно) держался на
-- set в памяти процесса, а in_operation писался вдогонку и авторитетом не был.
-- Вторая реплика видела свой пустой set и брала те же аккаунты. Теперь захват —
-- атомарный UPDATE ... RETURNING по этим колонкам, общий для всех процессов.
--
-- op_lease_owner — кто держит (id реплики), op_lease_until — до какого момента.
-- Аренда истекает сама: упавший процесс освобождает аккаунты без сторожа.

ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS op_lease_until TIMESTAMPTZ;
ALTER TABLE tg_accounts ADD COLUMN IF NOT EXISTS op_lease_owner TEXT;

-- Частичный индекс: горячие запросы — «чьи аренды протухли» и «что держит эта
-- реплика». Оба смотрят только занятые строки, поэтому WHERE in_operation.
CREATE INDEX IF NOT EXISTS idx_tg_accounts_lease
    ON tg_accounts (op_lease_until)
    WHERE in_operation = TRUE;

CREATE INDEX IF NOT EXISTS idx_tg_accounts_lease_owner
    ON tg_accounts (op_lease_owner)
    WHERE in_operation = TRUE;
