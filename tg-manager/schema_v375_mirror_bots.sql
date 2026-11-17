-- schema_v375: бот-зеркало владельца (личный запасной вход в панель).
--
-- Если основной бот Infragram забанят/ограничат, владелец теряет вход в панель
-- управления флотом. Зеркало — его собственный бот (токен из @BotFather), у
-- которого кнопка меню открывает тот же мини-апп. Падение основного бота больше
-- не отрезает владельца от управления.
--
-- Зеркало ведётся БЕЗ отдельного aiogram-инстанса: достаточно один раз выставить
-- боту web-app кнопку меню (setChatMenuButton на наш MINI_APP_URL?m=<id>). При
-- открытии мини-апп подписан токеном зеркала — /auth проверяет initData этим
-- токеном, но сессию выдаёт основным токеном, поэтому остальные ручки работают
-- без изменений.
--
-- token_enc — токен бота, шифрованный token_vault (как у managed_bots). Одно
-- зеркало на владельца (UNIQUE owner_id): смена токена обновляет строку, id
-- остаётся, чтобы ссылка кнопки меню (?m=<id>) не менялась.
CREATE TABLE IF NOT EXISTS mirror_bots (
    id          BIGSERIAL PRIMARY KEY,
    owner_id    BIGINT NOT NULL,
    bot_id      BIGINT NOT NULL,
    username    TEXT,
    first_name  TEXT,
    token_enc   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_mirror_bots_owner ON mirror_bots(owner_id);
CREATE INDEX IF NOT EXISTS idx_mirror_bots_bot_id ON mirror_bots(bot_id);
