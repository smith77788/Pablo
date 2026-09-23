-- Когда последний раз сверяли имя и @username бота с Telegram.
--
-- Зачем: managed_bots.first_name и managed_bots.username записывались ОДИН раз,
-- при подключении бота, из getMe. Больше их не трогал никто. Имя бота меняется
-- в @BotFather, а не через Infragram, поэтому после переименования список ботов
-- навсегда показывал старое имя, а изменённый @username делал подпись на
-- карточке нерабочей ссылкой.
--
-- Колонка нужна именно как отметка прохода: без неё фоновая сверка либо
-- опрашивала бы getMe на каждом круге поллинга (бессмысленный запрос — имя
-- меняют раз в месяцы), либо крутилась бы вокруг одних и тех же ботов,
-- никогда не доходя до остальных.

ALTER TABLE managed_bots
    ADD COLUMN IF NOT EXISTS profile_checked_at TIMESTAMPTZ;

-- Выборка всегда «кого не сверяли дольше всех»: сначала NULL, затем по давности.
CREATE INDEX IF NOT EXISTS idx_managed_bots_profile_checked
    ON managed_bots(profile_checked_at NULLS FIRST)
    WHERE is_active = TRUE;
