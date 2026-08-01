-- Привязка бота к аккаунту-создателю.
-- managed_bots.acc_id → tg_accounts.id аккаунта, через который бот создан в
-- Bot Factory (@BotFather). Нужна, чтобы карточка аккаунта показывала его «свои»
-- боты, а не весь портфель владельца. NULL = бот добавлен по токену вручную
-- (владельцем, без конкретного аккаунта) — таких показываем только в общем списке.
ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS acc_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_managed_bots_acc_id ON managed_bots(acc_id);
