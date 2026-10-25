-- Virtual Channel Administrator — переходы с канала-витрины на основной канал.
--
-- ЗАЧЕМ. Администратор учился только на просмотрах, реакциях и пересылках, а
-- задача витрины — приводить людей на основной ресурс. Если основной ресурс —
-- канал или группа владельца в Infragram, в продающий пост вместо @основного
-- ставится СВОЯ ссылка-приглашение (exportChatInvite с названием поста), и
-- Telegram сам считает, сколько людей вступило именно по ней. Это не клики, а
-- реальные вступления в основной канал, по каждому посту и рубрике.
-- Только CREATE ... IF NOT EXISTS, идемпотентно (governance: миграции не переигрываются).

CREATE TABLE IF NOT EXISTS va_tracked_links (
    id                BIGSERIAL   PRIMARY KEY,
    owner_id          BIGINT      NOT NULL,
    channel_id        BIGINT      NOT NULL,   -- канал-витрина (va_channel_admin.channel_id)
    target_channel_id BIGINT      NOT NULL,   -- основной канал (managed_channels.channel_id)
    link              TEXT,                   -- t.me/+…; NULL, пока ссылка не создана
    pillar            TEXT,
    op_id             BIGINT,                 -- публикация поста со ссылкой
    joins             INTEGER     NOT NULL DEFAULT 0,  -- вступили по ссылке (usage)
    requests          INTEGER     NOT NULL DEFAULT 0,  -- заявки на вступление (requested)
    checked_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_va_tracked_links_channel
    ON va_tracked_links(owner_id, channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_va_tracked_links_target
    ON va_tracked_links(owner_id, target_channel_id, created_at DESC);
