-- schema_v161: журнал уже-приглашённых целей для дедупа инвайтера.
--
-- Повторный запуск инвайта по той же группе снова тыкал тех, кого уже
-- обрабатывали: это впустую сжигает суточный лимит аккаунтов и растит частоту
-- PeerFlood (инвайт — самая баноопасная операция). Ключ — (владелец,
-- нормализованная группа); пишем цели, реально отданные движку (успех/отказ =
-- «уже трогали», повторно поке не подлежат). Дедуп включён по умолчанию,
-- отключается параметром операции skip_invited=false.
--
-- Таблица также создаётся защитно (CREATE TABLE IF NOT EXISTS) в
-- op_worker._load_invited_targets — эта миграция лишь канонизирует её.

CREATE TABLE IF NOT EXISTS invite_target_log (
    owner_id   BIGINT      NOT NULL,
    group_key  TEXT        NOT NULL,
    target     TEXT        NOT NULL,
    op_id      BIGINT,
    created_at TIMESTAMPTZ  DEFAULT now(),
    PRIMARY KEY (owner_id, group_key, target)
);

CREATE INDEX IF NOT EXISTS idx_invite_target_log_owner_group
    ON invite_target_log (owner_id, group_key);
