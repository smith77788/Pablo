-- v196: честный статус плана прогрева.
--
-- Экран прогрева знал о плане только `status` и «День X/Y». Движок при этом
-- молча пропускал день (аккаунт занят операцией, выключен, забанен, в
-- спам-блоке, сессия протухла) — план оставался active, день не рос, и
-- пользователь неделями видел «🟢 Активен · День 3/21» на мёртвом аккаунте.
-- А паузу, поставленную самим движком при бане или спам-блоке, было не
-- отличить от паузы, поставленной человеком: пользователь жал «Возобновить»
-- и добивал флагнутый аккаунт.
--
-- Эти колонки дают плану память о том, ПОЧЕМУ он стоит.
ALTER TABLE account_warmup_plans
    ADD COLUMN IF NOT EXISTS pause_reason      TEXT,
    ADD COLUMN IF NOT EXISTS pause_detail      TEXT,
    ADD COLUMN IF NOT EXISTS paused_at         TIMESTAMPTZ,
    -- О самостоятельной остановке сообщаем владельцу ОДИН раз (тот же приём,
    -- что у сторожа прокси): иначе уведомление повторялось бы каждый цикл.
    ADD COLUMN IF NOT EXISTS pause_notified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_skip_reason  TEXT,
    ADD COLUMN IF NOT EXISTS last_skip_at      TIMESTAMPTZ;

-- Сторож уведомлений берёт только самостоятельно остановленные планы,
-- о которых ещё не сообщали.
CREATE INDEX IF NOT EXISTS idx_warmup_pause_unnotified
    ON account_warmup_plans(owner_id)
 WHERE status = 'paused' AND pause_notified_at IS NULL;
