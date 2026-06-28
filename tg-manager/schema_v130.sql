-- v130: переименование старого бренда BotMother → Infragram в уже засеянных
-- шаблонах самопиара (контент/заголовок/CTA). Сиды schema_v101 правились, но
-- существующие строки в БД остались со старым названием.

UPDATE self_promo_templates
SET title    = REPLACE(title, 'BotMother', 'Infragram'),
    content  = REPLACE(content, 'BotMother', 'Infragram'),
    cta_text = REPLACE(COALESCE(cta_text, ''), 'BotMother', 'Infragram')
WHERE title LIKE '%BotMother%'
   OR content LIKE '%BotMother%'
   OR cta_text LIKE '%BotMother%';

-- Также вычищаем захардкоженные ссылки на старого бота из контента/CTA, если
-- остались (worker подставит реальную реф-ссылку при запуске).
UPDATE self_promo_templates
SET content  = REPLACE(REPLACE(content, 'https://t.me/BotMotherBot', ''), 't.me/BotMotherBot', ''),
    cta_url  = NULLIF(REPLACE(COALESCE(cta_url, ''), 'https://t.me/BotMotherBot', ''), '')
WHERE content LIKE '%BotMotherBot%'
   OR cta_url LIKE '%BotMotherBot%';
