-- Облако: ИЗБЫТОЧНОСТЬ кусков — гарантия доступа к файлам несмотря на баны флота.
-- Каждый кусок хранится в НЕСКОЛЬКИХ местах (репликах) на РАЗНЫХ аккаунтах-
-- хранителях. Бан одного аккаунта помечает его локации dead; сборка файла берёт
-- любую живую реплику, а реконсайлер (heal) восстанавливает избыточность,
-- перекладывая недостающие копии с уцелевших. Так файл не теряется, даже если
-- часть флота забанена.
--
-- Доступ к облаку (has_access) и к скачиванию НИКОГДА не зависит от здоровья
-- аккаунтов флота — только от владельца/оплаты. Бан хранителей влияет лишь на
-- то, откуда взять байты, но не на право их получить.

CREATE TABLE IF NOT EXISTS tg_cloud_chunk_locs (
    id          BIGSERIAL PRIMARY KEY,
    chunk_id    BIGINT NOT NULL REFERENCES tg_cloud_chunks(id) ON DELETE CASCADE,
    owner_id    BIGINT NOT NULL,
    replica     INT    NOT NULL DEFAULT 0,        -- номер реплики (0..N-1)
    acc_id      BIGINT,          -- аккаунт-хранитель (tg_accounts.id); NULL для БД-бэкенда
    channel_id  BIGINT,
    message_id  BIGINT,
    locator     TEXT   NOT NULL,                  -- ключ хранения (транспорт-специфично)
    status      TEXT   NOT NULL DEFAULT 'stored', -- stored|dead (бан/потеря)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(chunk_id, locator)
);
CREATE INDEX IF NOT EXISTS idx_tg_cloud_locs_chunk ON tg_cloud_chunk_locs(chunk_id, status);
-- Поиск локаций аккаунта (при бане помечаем их dead одним UPDATE).
CREATE INDEX IF NOT EXISTS idx_tg_cloud_locs_acc ON tg_cloud_chunk_locs(acc_id) WHERE acc_id IS NOT NULL;
