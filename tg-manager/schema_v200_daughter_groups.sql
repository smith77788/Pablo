-- v200: «Мать-Дочка» — одноразовые группы поглощают бан-риск инвайта.
--
-- Массовый инвайт — самая баноопасная операция продукта. Идея: приглашать
-- аудиторию не сразу в боевую (мать) группу, а в одноразовую «дочернюю»,
-- специально созданную под кампанию, с закреплённым сообщением-редиректом на
-- мать. Если Telegram закрывает дочернюю группу (ChatWriteForbiddenError/
-- ChannelPrivateError на инвайте — уже классифицируется движком как «group
-- error» в mass_inviter_engine.classify_invite_error) — сгорает расходник, а
-- не боевой канал; op_worker подхватывает следующую активную дочернюю группу
-- и продолжает прогон, не начиная кампанию заново.
--
-- Одна активная дочерняя группа на пару (owner, mother) — не плодим лишние
-- каналы, пока текущая жива.
CREATE TABLE IF NOT EXISTS daughter_groups (
    id                  BIGSERIAL PRIMARY KEY,
    owner_id            BIGINT NOT NULL,
    mother_ref          TEXT NOT NULL,        -- боевая группа назначения (как её передали в операцию)
    group_ref           TEXT NOT NULL,        -- invite-ссылка дочерней группы (резолвится любым аккаунтом)
    channel_id          BIGINT NOT NULL,
    access_hash         BIGINT NOT NULL DEFAULT 0,
    creator_account_id  BIGINT NOT NULL,      -- каким аккаунтом создана (владеет правами админа)
    status              TEXT NOT NULL DEFAULT 'active',  -- active|burned
    burn_reason         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    burned_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_daughter_groups_active
    ON daughter_groups(owner_id, mother_ref, status);
