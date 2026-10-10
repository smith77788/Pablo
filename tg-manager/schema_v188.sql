-- Авто-реабилитация ограниченных аккаунтов (спам-блок → тихий прогрев →
-- перепроверка → возврат в строй).
-- Зачем: аккаунт под спам-блоком выпадал из ВСЕГО — операции его не берут
-- (ресурс-селектор фильтрует dead-статусы), и даже штатный прогрев его пропускает
-- (run_warmup_session гейтит spamblock). То есть ограниченный аккаунт замирал
-- навсегда и ждал ручного решения владельца. Этот стейт-машина ведёт его сама:
-- запрос снятия у @SpamBot → тихий пассивный прогрев (только чтение, без рассылок,
-- что при спам-блоке легитимно) → периодическая перепроверка статуса → при снятии
-- возврат acc_status='active'. Состояние переживает рестарт процесса.
-- См. services/account_rehab.py.
CREATE TABLE IF NOT EXISTS account_rehab_state (
    acc_id          BIGINT PRIMARY KEY,     -- tg_accounts.id
    owner_id        BIGINT NOT NULL,
    -- Фаза стейт-машины: appeal → warming → recheck → freed|stuck|gone
    phase           TEXT NOT NULL DEFAULT 'appeal',
    kind            TEXT,                   -- 'temp' | 'perm' (по ответу @SpamBot)
    attempts        INTEGER NOT NULL DEFAULT 0,   -- перепроверок сделано
    appeal_count    INTEGER NOT NULL DEFAULT 0,   -- аппеляций отправлено
    warm_cycles     INTEGER NOT NULL DEFAULT 0,   -- циклов тихого прогрева
    warm_actions    INTEGER NOT NULL DEFAULT 0,   -- пассивных действий всего
    note            TEXT,                   -- последний человекочитаемый итог
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_action_at  TIMESTAMPTZ,
    next_action_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    freed_at        TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_account_rehab_due
    ON account_rehab_state(next_action_at)
    WHERE phase IN ('appeal', 'warming', 'recheck');
