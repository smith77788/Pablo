-- Сохранённые сегменты: умный фильтр контактов как СУЩНОСТЬ. Один срез
-- («Горячие», «Молчуны 30д») переиспользуется и рассылкой, и инвайтом — единый
-- таргетинг «вижу = действую». filters — тот же JSON, что понимает _segment_where.
CREATE TABLE IF NOT EXISTS saved_segments (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    name        TEXT NOT NULL,
    filters     JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_saved_segments_owner ON saved_segments(owner_id, created_at DESC);
