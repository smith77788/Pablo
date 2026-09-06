-- Внешняя инфраструктура цели в истории Strike.
-- Живая сводка удара уже показывает 🕸 «доменов N · APWG M · регистратору K»
-- (StrikeResult.infra_domains/infra_apwg_sent/infra_registrar_sent), но вкладка
-- «История» это не хранила — по завершённому удару не видно, что внешние площадки
-- (платёжки/фишинг) реально отработаны через APWG/Safe Browsing/регистратора.
-- Добавляем счётчики в strike_history, чтобы история отражала весь пул векторов.
-- Аддитивно, DEFAULT 0 — старые вставки и чтения не ломаются.
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_domains        INT DEFAULT 0;
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_apwg_sent      INT DEFAULT 0;
ALTER TABLE strike_history ADD COLUMN IF NOT EXISTS infra_registrar_sent INT DEFAULT 0;
