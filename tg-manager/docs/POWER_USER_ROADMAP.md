# POWER-USER ROADMAP — вывод инфраструктуры в топ Telegram-поиска + сетки

Взгляд владельца крупной инфраструктуры (сетки каналов/чатов/ботов + SEO в
Telegram-поиске). Составлено по факту кода 2026-07-09, сравнение с TeleRaptor /
TgSoft / Telegram Expert. Правило: НЕ дублировать — сначала расширять
существующее. Легенда: ✅ есть · 🟡 частично · ⛔ нет.

## Что УЖЕ есть (сильная база, дублировать НЕ надо)

**Ранжирование/SEO:** трекинг позиций по ключам+регионам (`ranking.py` 1876
строк, `position_history`, `search_rankings`, `search_snapshots`), SEO-скоринг
(`entity_analyzer._calc_seo`, 0-100 + заметки), AI-предложения title/about/
username (`seo_ai_suggestions`), дашборд видимости, тренды, алерты об изменении
позиций (`search_change_events`), трекинг конкурентов (`competitors.py`).

**Накрутка сигналов ранжирования:** `boost_subscribers`, `boost_views`,
`boost_reactions`, `boost_stories`, `boost_bot_starts`.

**Присутствие/сеть:** `global_presence_bot/channel/group`,
`seed_presence_pack`, `promote_presence_pack`, `niche_growth_post`,
`pin_last_post`, экосистемы (`ecosystem_brain`), Content Mesh (авто-кросс-постинг
по loop), Swarm-метрики.

**Создание/управление сеткой:** `create_channel`, `create_group`,
`bulk_create_channels`, `bulk_edit_channels`, `bulk_bot_edit`, `channel_import_all`,
`promote_all_admins`, Bot Factory, редактирование канала (title/about/username в
`account_manager`).

**Инфраструктура:** аккаунты+прогрев+прокси-изоляция+device-fingerprint,
парсер аудитории, инвайтер, авто-ответы, DM-кампании, авто-регистрация+SMS.

## ГЭПЫ (записано в память → реализовать до Top-1)

### Приоритет 1 — «замкнуть петлю SEO» (ПОДТВЕРЖДЁН, в работе)
⛔ **One-click «Применить SEO-предложение».** Есть анализ→рекомендация
(`seo_ai_suggestions`: title/about/username на канал), но НЕТ действия
применения — оптимизацию нужно вбивать вручную по каждому каналу. Конкуренты
(Telegram Expert) дают «применить оптимизацию» в один клик.
Реализация через существующее: `edit_channel_title/about` + `set_channel_username`
(уже есть в account_manager) + резолв управляющего аккаунта через
`managed_channels.acc_id`. → эндпоинт `POST /api/miniapp/seo/apply` + кнопка на
экране SEO. **СТАТУС: реализуется первым.**

### Приоритет 2 — массовость и автоматизм SEO (кандидаты, проверить)
🟡 **Bulk-применение SEO по всей сетке** — применить рекомендации ко всем
каналам сети разом (сейчас правки — по одному; `bulk_edit_channels` ставит ОДНО
значение на все, не per-channel рекомендации).
✅ **Авто-реоптимизация при падении позиции** — РЕАЛИЗОВАНО для СТОРОНЫ БОТОВ
(ранжирование бот-центрично: `tracked_keywords.bot_id`/`position_history.bot_id`;
у каналов петля SEO уже была). `ranking_checker._check_visibility_alerts` на
падении ниже порога (opt-in `visibility_alert_settings.auto_reoptimize`) генерит
рекомендацию по имени/краткому описанию бота с просевшим ключом
(`services/bot_reoptimizer.py`, чистая логика + `bot_seo_suggestions`), кладёт в
алерт и в раздел SEO мини-аппа → применение оператором в один клик
(`POST /api/miniapp/seo/apply_bot`, те же методы Bot API `setMyName`/
`setMyShortDescription`, что и bulk_bot_edit). Фонового авто-переименования НЕТ
(полудеструктивно). Регресс: `tests/test_bot_reoptimizer.py` (13 тестов).
🟡 **A/B-тест названий/ключей для КАНАЛОВ** — `stars_experiments` есть для
ботов (CTR старта), для каналов A/B заголовков/юзернеймов нет.

### Приоритет 3 — оркестрация сетки (кандидаты, проверить)
🟡 **Шаблоны сетей** — развернуть тематическую сетку (N каналов + линкед-чаты +
базовое SEO + расписание) одним действием (есть отдельные кирпичи
create_channel/create_group, нет сборки «под ключ»).
🟡 **Авто-линковка обсуждения (discussion chat)** при создании канала.
🟡 **Сетевой health/analytics-дашборд** — сводка по всей сетке (позиции,
охваты, здоровье аккаунтов) в одном экране.

### Приоритет 4 — что есть у конкурентов и стоит сверить
- Массовая простановка/ротация аватаров+эмодзи-статусов по сетке (брендинг).
- Планировщик «умного» постинга под пиковые часы аудитории (есть Audience DNA
  peak_hours — не факт, что связан с планировщиком постинга).
- Экспорт/бэкап всей конфигурации сетки.

> Оффенсив-модули (Strike/масс-жалобы) — вне этого роадмапа: доводятся только по
> техкорректности, без наращивания takedown-эффективности (см. AUDIT_LEDGER).
