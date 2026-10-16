-- v16: Дополнительные индексы для tg_accounts и search_rankings

-- Индекс для быстрого поиска активных аккаунтов владельца (используется ranking_checker'ом)
CREATE INDEX IF NOT EXISTS idx_tg_accounts_owner_active
    ON tg_accounts(owner_id, is_active);

-- Индекс для сортировки аккаунтов по last_used (get_active_account_for_owner)
CREATE INDEX IF NOT EXISTS idx_tg_accounts_last_used
    ON tg_accounts(owner_id, last_used DESC NULLS LAST)
    WHERE is_active = TRUE;

-- Индекс для search_rankings по bot_id (дополнительно к keyword_id)
CREATE INDEX IF NOT EXISTS idx_rankings_bot
    ON search_rankings(bot_id, checked_at DESC);
