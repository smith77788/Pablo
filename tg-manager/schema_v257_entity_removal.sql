-- v257: полное удаление и проверка живости ботов и каналов/чатов.
--
-- Бот. До этой миграции «удалить» и «поставить на паузу» писали один и тот же
-- флаг is_active, поэтому удалённый бот оставался в списке мини-аппа и
-- возвращался кнопкой «Вернуть в работу». deleted_at отделяет удаление от
-- паузы: бот пропадает из всех списков, а работа его и так не берёт (is_active
-- снимается тем же запросом). Строка остаётся: на managed_bots висит каскад
-- bot_users/воронок/рассылок, и повторное добавление тем же токеном
-- возвращает бота вместе с аудиторией.
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

-- Канал/чат. Строка managed_channels — кэш того, что видят аккаунты, и импорт
-- диалогов возвращал удалённый владельцем канал обратно. Удалённые и пропавшие
-- в Telegram каналы записываются сюда:
--   reason='owner' — убран владельцем, импорт его больше не возвращает;
--   reason='gone'  — проверка увидела, что канал удалён в Telegram или аккаунт
--                    потерял к нему доступ (бан/исключение). Если канал снова
--                    найдётся в диалогах, импорт вернёт его и снимет запись.
-- Колонки acc_id/access_hash/… нужны, чтобы «Вернуть» восстановило строку.
CREATE TABLE IF NOT EXISTS removed_channels (
    owner_id      BIGINT      NOT NULL,
    channel_id    BIGINT      NOT NULL,
    reason        TEXT        NOT NULL CHECK (reason IN ('owner', 'gone')),
    detail        TEXT        NOT NULL DEFAULT '',
    acc_id        INTEGER,
    title         TEXT,
    username      TEXT,
    type          TEXT,
    access_hash   BIGINT      NOT NULL DEFAULT 0,
    removed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, channel_id)
);

-- Сколько проверок подряд канал отвечал «недоступен». Одна ошибка — ещё не
-- приговор: убираем из работы только после подтверждения следующим кругом.
ALTER TABLE managed_channels ADD COLUMN IF NOT EXISTS gone_streak INTEGER NOT NULL DEFAULT 0;
