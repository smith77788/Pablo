-- Легитимные жалобы по почте — журнал обращений (метаданные, НЕ материал).
--
-- Одна достоверная жалоба оператора со своего подключённого ящика в официальный
-- abuse-адрес платформы. Здесь хранится только МЕТА: цель, категория, ссылки/ID
-- доказательств (evidence jsonb — строки-ссылки, не контент), контекст, адресат,
-- честный статус и время отправки. Сам запрещённый материал не хранится никогда.
-- Статусы: no_mailbox | sent | send_failed (в коде также invalid — до записи).
-- «sent» НЕ означает блокировку: решение за модерацией платформы.
-- Только ADD/CREATE, идемпотентно.

CREATE TABLE IF NOT EXISTS abuse_reports (
    id          BIGSERIAL   PRIMARY KEY,
    owner_id    BIGINT      NOT NULL,
    ref_id      TEXT        NOT NULL,              -- человекочитаемый номер обращения
    target      TEXT        NOT NULL,              -- @username / t.me / id
    category    TEXT        NOT NULL,              -- из закрытого списка CATEGORIES
    evidence    JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- ССЫЛКИ/ID, не материал
    context     TEXT        NOT NULL DEFAULT '',
    recipient   TEXT        NOT NULL DEFAULT '',   -- куда ушло (abuse-дески)
    status      TEXT        NOT NULL DEFAULT 'draft',
    error       TEXT,
    sent_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_abuse_reports_owner
    ON abuse_reports(owner_id, created_at DESC);
