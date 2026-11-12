-- schema_v343 (было v317 — номер занят самопиаром; DDL идемпотентный, повторный прогон на проде безопасен): состояние диалога «Хранилища» — то, что владелец делает с чатом.
--
-- Хранилище было голым архивом сообщений: ни «прочитано», ни «закреплено», ни
-- «в архиве», ни «уже ответил по телефону» записать было некуда. Поэтому
-- напоминание «N клиентов ждут ответа» считалось только по направлению
-- последнего сообщения и приходило про людей, которым уже ответили другим
-- путём или которые уже купили. Здесь — одна строка на (владелец, чат):
--   read_at       — до какого момента владелец прочитал (непрочитанные считаются после);
--   handled_at    — «отвечено/закрыто» вручную: входящие до этой отметки не ждут ответа;
--   purchased_at  — клиент воспользовался услугой: не ждёт ответа и не «ходит по кругу»;
--   pinned / archived / muted — как в списке чатов Telegram;
--   note          — заметка владельца к диалогу.
CREATE TABLE IF NOT EXISTS vault_chat_state (
    owner_id      BIGINT NOT NULL,
    chat_id       BIGINT NOT NULL,
    pinned        BOOLEAN NOT NULL DEFAULT FALSE,
    archived      BOOLEAN NOT NULL DEFAULT FALSE,
    muted         BOOLEAN NOT NULL DEFAULT FALSE,
    read_at       TIMESTAMPTZ,
    handled_at    TIMESTAMPTZ,
    purchased_at  TIMESTAMPTZ,
    note          TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, chat_id)
);
