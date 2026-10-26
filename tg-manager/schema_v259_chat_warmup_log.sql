-- schema_v259: журнал ходов разогрева чата — кто из флота что написал и почему промолчал.
--
-- До этого у разогрева были только счётчики в chat_warmup_sessions
-- (messages_sent, last_speaker, last_run_at). Для владельца это значило вот что:
-- строка «активен · смешанный · отправлено 0» и никакой возможности узнать, что
-- происходит. А промолчать ход может по десятку причин — аккаунт на кулдауне, в
-- карантине, занят операцией, флот не смог вступить в чат, ночное окно,
-- LLM недоступен, текст отклонён проверкой контента, Telegram попросил паузу —
-- и ни одна из них нигде не оставалась.
--
-- Это ещё и нарушение жёсткого гейта продукта: массовая операция обязана иметь
-- per-target лог и честный счётчик. Разогрев — массовая операция по живым
-- аккаунтам, и журнала у неё не было вовсе.
--
-- Строка пишется на КАЖДЫЙ ход: и на удачный (status='ok'), и на пропущенный
-- (status='skipped', с причиной), и на сорвавшийся (status='error'). Подряд
-- идущие пропуски с ОДНОЙ причиной не плодят строки: у последней растёт
-- `repeats` и обновляется `created_at`. Иначе сессия, которая не может войти в
-- чат, за сутки писала бы тысячи одинаковых строк, а владельцу всё равно нужно
-- одно: что мешает прямо сейчас и давно ли.

CREATE TABLE IF NOT EXISTS chat_warmup_log (
    id          BIGSERIAL PRIMARY KEY,
    session_id  INT    NOT NULL,
    owner_id    BIGINT NOT NULL,
    account_id  BIGINT,                       -- кто говорил (NULL — ход не дошёл до выбора)
    kind        TEXT   NOT NULL DEFAULT 'turn',   -- reply | seed | reaction | turn
    status      TEXT   NOT NULL,              -- ok | skipped | error
    reason      TEXT,                         -- по-русски: почему промолчали или что сорвалось
    text        TEXT,                         -- что написали (для status='ok')
    reply_to    TEXT,                         -- кому отвечали: имя и начало сообщения
    repeats     INT    NOT NULL DEFAULT 1,     -- сколько ходов подряд дали ту же причину
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_warmup_log_session
    ON chat_warmup_log(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_warmup_log_owner
    ON chat_warmup_log(owner_id, created_at DESC);
-- Чистка по возрасту: журнал пишется на каждый ход каждой активной сессии.
CREATE INDEX IF NOT EXISTS idx_chat_warmup_log_created
    ON chat_warmup_log(created_at);
