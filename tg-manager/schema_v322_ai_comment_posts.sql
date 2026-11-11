-- schema_v322: посты, под которыми ИИ-комментинг владельца уже писал.
--
-- Защита от повтора у ИИ-комментинга жила только внутри одной операции
-- (журнал по каналу), поэтому второй запуск по тому же списку каналов писал
-- ещё один комментарий под тот же свежий пост — пара похожих реплик от
-- аккаунтов одного владельца под одним постом и есть подпись накрутки.
-- Строка ставится ДО отправки и является её ценой (ON CONFLICT DO NOTHING:
-- не вставилась — комментария не будет).
CREATE TABLE IF NOT EXISTS ai_comment_posts (
    owner_id    BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,
    post_id     BIGINT NOT NULL,
    op_id       BIGINT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, channel_id, post_id)
);
CREATE INDEX IF NOT EXISTS idx_ai_comment_posts_created ON ai_comment_posts(created_at);
