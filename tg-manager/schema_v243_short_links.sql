-- v243: таблица сокращателя ссылок.
--
-- Сокращатель («bit.ly для себя») был подключён целиком: эндпойнты мини-аппа
-- создают, перечисляют, отключают и удаляют ссылку, публичный маршрут
-- /s/{code} их раздаёт и считает клики. Таблицу же не создавала ни одна
-- миграция — её заводила только `link_shortener.ensure_table`, которую НИКТО
-- не вызывает. Запрос падал, `except Exception` превращал падение в «данных
-- нет», и раздел выглядел рабочим, не работая ни в одной своей части. Ровно
-- тот же случай, что с мёртвым разделом «Воркфлоу» (см.
-- tests/test_sql_tables_exist_in_schema.py).
--
-- Колонки — те же, что в DDL модуля, вместе с добавленными позже expires_at и
-- tags: там это ALTER ... IF NOT EXISTS поверх таблицы прошлого релиза, здесь
-- сразу финальный вид.
CREATE TABLE IF NOT EXISTS short_links (
    code          TEXT PRIMARY KEY,                 -- хвост короткой ссылки
    owner_id      BIGINT NOT NULL,
    target_url    TEXT NOT NULL,
    title         TEXT,
    clicks        BIGINT NOT NULL DEFAULT 0,
    disabled      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_click_at TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,
    tags          TEXT
);
-- Список ссылок владельца — свежие сверху: единственная выборка раздела.
CREATE INDEX IF NOT EXISTS idx_short_links_owner
    ON short_links(owner_id, created_at DESC);
