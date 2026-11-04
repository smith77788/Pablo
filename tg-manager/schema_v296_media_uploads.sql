-- schema_v296: картинки и видео с устройства для полей «ссылка на медиа».
--
-- ЧТО БЫЛО. Аватар бота или аккаунта, фото к посту, вложение шага сценария
-- задавались только публичной https-ссылкой. Владелец с телефона не может
-- выложить картинку куда-то в интернет ради ссылки — функции были для него
-- недоступны.
--
-- ЧТО ЗДЕСЬ. media_uploads — файл, загруженный из мини-аппа. Отдаётся по
-- длинной случайной ссылке /media/u/{token} (её и подставляет экран в поле),
-- поэтому всё, что уже умеет брать медиа по ссылке, работает без изменений.
-- Только картинки и видео (тип проверяется по содержимому), живёт 7 дней.
CREATE TABLE IF NOT EXISTS media_uploads (
    token       TEXT PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    mime        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    data        BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_media_uploads_owner
    ON media_uploads (owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_uploads_created
    ON media_uploads (created_at);
