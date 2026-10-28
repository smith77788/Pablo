-- Снимки «как было» для отката массовых изменений профиля (боты, а дальше —
-- каналы и чаты по тому же механизму). Владелец выбрал «максимально безопасно»:
-- предпросмотр до применения и откат «вернуть как было». Эта таблица — основа
-- отката: операция применения ПЕРЕД изменением сохраняет сюда прежние значения
-- каждой цели, а операция отката их восстанавливает.
--
-- Универсально по типу сущности (entity_type), чтобы каналы/чаты переиспользовали
-- тот же путь. before — JSONB прежних значений ТОЛЬКО изменяемых полей.
-- reverted_at: NULL — снимок ещё можно откатить; проставляется при откате.
CREATE TABLE IF NOT EXISTS bulk_edit_snapshots (
    id SERIAL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    op_id BIGINT,                      -- операция применения, создавшая снимок
    entity_type TEXT NOT NULL,         -- 'bot' | 'channel' | 'group'
    target_id BIGINT NOT NULL,         -- bot_id / channel_id
    before JSONB NOT NULL DEFAULT '{}'::jsonb,
    reverted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bes_owner_op
    ON bulk_edit_snapshots(owner_id, op_id);
CREATE INDEX IF NOT EXISTS idx_bes_owner_entity
    ON bulk_edit_snapshots(owner_id, entity_type, created_at DESC);
