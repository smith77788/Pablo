-- schema_v382: секретное слово владельца против фишинга (owner_safeword.py).
--
-- Слово владелец задаёт сам; каждая наша тревога его показывает, поэтому
-- сообщение без слова — подделка. Это метка подлинности, а не пароль: её надо
-- ПОКАЗАТЬ, поэтому хранится как есть (в отличие от кода Infragram — scrypt).
CREATE TABLE IF NOT EXISTS owner_safeword (
    owner_id   BIGINT      PRIMARY KEY,
    phrase     TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
