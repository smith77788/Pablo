-- v211: Virtual Layer — динамические состояния и виртуальные события.
--
-- Организм уже имеет шину событий (organism_events) и общий контекст владельца
-- (organism_state, ключ→значение). Чего не было — СОСТОЯНИЯ как самостоятельной
-- динамической сущности: не тег «interested», а величина с уверенностью,
-- источником, сроком жизни и историей переходов, адресуемая на любом уровне
-- (пользователь / диалог / бот / кампания / сеть).
--
-- Это и есть разрыв, который отделяет «автоответчик по событию» от модели
-- поведения: событие говорит «что произошло», состояние — «в каком положении
-- система сейчас». На переходах состояния рождаются виртуальные события
-- (PURCHASE_INTENT_DETECTED, USER_LOST_INTEREST), которых Telegram не шлёт.

-- Текущее состояние сущности по одному ключу (напр. key='funnel').
-- Не источник истины поверх unified_contacts.stage, а НАДСТРОЙКА: сюда пишутся
-- вычисляемые состояния с уверенностью и распадом, которых у сырого тега нет.
CREATE TABLE IF NOT EXISTS virtual_states (
    owner_id     BIGINT NOT NULL,
    entity_type  TEXT   NOT NULL,           -- user|conversation|bot|campaign|network
    entity_id    TEXT   NOT NULL,           -- id сущности (строкой: user_id, bot_id, …)
    state_key    TEXT   NOT NULL,           -- какое измерение состояния (funnel|dialog|…)
    value        TEXT   NOT NULL,           -- значение на лестнице состояний
    confidence   REAL   NOT NULL DEFAULT 0.5,   -- 0..1, насколько уверены
    source       TEXT,                      -- кто выставил (bot_17, sensor, cascade)
    -- Распад: после срока состояние деградирует само (тишина = остывание).
    -- NULL — бессрочное (терминальные вроде purchased/lost).
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, entity_type, entity_id, state_key)
);
CREATE INDEX IF NOT EXISTS idx_vstates_owner_key
    ON virtual_states(owner_id, entity_type, state_key, value);
-- Очередь распада: «кого пора остудить».
CREATE INDEX IF NOT EXISTS idx_vstates_expiry
    ON virtual_states(expires_at) WHERE expires_at IS NOT NULL;

-- История переходов. Только дописывается: по ней считается «сколько раз человек
-- заходил и уходил» — сам по себе поведенческий сигнал (temporal layer).
CREATE TABLE IF NOT EXISTS virtual_state_history (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    entity_type  TEXT   NOT NULL,
    entity_id    TEXT   NOT NULL,
    state_key    TEXT   NOT NULL,
    from_value   TEXT,
    to_value     TEXT   NOT NULL,
    reason       TEXT,                       -- сигнал/распад/каскад
    confidence   REAL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_vstate_hist_entity
    ON virtual_state_history(owner_id, entity_type, entity_id, state_key, created_at);
