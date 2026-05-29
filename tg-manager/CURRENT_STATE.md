# CURRENT STATE

Обновлено: 2026-05-29 (r8)

## Статус: АКТИВНАЯ РАЗРАБОТКА

### ✅ КОНСОЛИДАЦИЯ: BotMother OS — единственная точка входа

Все 6 прямых команд заменены на redirect в BotMother OS:
- `/ai` → BotMother → 🤖 AI Assistant
- `/accounts` → BotMother → 🏗️ Infrastructure → 📱 Аккаунты
- `/ops` → BotMother → 🏗️ Infrastructure → 📡 Каналы & операции
- `/ranking` → BotMother → 👁️ Visibility → 📊 Позиции
- `/referral` → BotMother → 💳 Billing → 👥 Referral
- `/subscription` → BotMother → 💳 Billing

**Коммиты:** 9ddf27f (основная) + 549a339 (deploy trigger)

### ✅ Выполнено в текущей сессии

#### Спринт 1: Консолидация и V2
1. **BotMother OS consolidation** — ВСЕ команды → меню
   - Коммит: 9ddf27f (основной) + 549a339 (deploy trigger v2026.05.28-r3)
   - Все 6 команд переведены на redirect с понятным объяснением

2. **Global Presence Factory V2** — поддержка ГРУПП
   - Коммит: f7719f0
   - Включена кнопка 👥 Группы в меню выбора типа актива
   - Универсальная функция _exec_global_presence_channel поддерживает оба типа
   - Параметр megagroup=True для групп, megagroup=False для каналов
   - Динамический текст FSM-шагов в зависимости от типа

3. **Operation Reports Enhancement** — улучшенная статистика
   - Коммит: 027cf95
   - Функции: get_operation_stats(), get_user_operation_history(), count_operation_errors()
   - Operation Reports UI уже был реализован в botmother_menu.py
   - Детальный анализ ошибок и производительности

#### Спринт 0: Исходная реализация (V1)
1. **Global Presence Factory V1** — ПОЛНОСТЬЮ РЕАЛИЗОВАН
   - `schema_v35.sql` — таблицы `global_presence_plans` + `global_presence_targets`
   - `services/geo_data.py` — 5 гео-пресетов (EU 44, World 51, Tier-1 50, DACH 20, LATAM 25 городов)
   - `services/username_engine.py` — transliterate + slugify + generate_username_variants
   - `services/presence_planner.py` — render_pattern() + build_targets() + estimate_duration_minutes()
   - `bot/handlers/global_presence.py` — полный FSM wizard 8 шагов + прогресс + отчёт + retry
   - `services/op_worker.py` — обработчик `global_presence_channel` с safe pacing 45-90с
   - `database/db.py` — 7 новых CRUD-функций для планов и целей
   - `bot/callbacks.py` — `GeoPresenceCb(prefix="gp")`
   - `bot/states.py` — `GlobalPresenceFSM`
   - `bot/handlers/botmother_menu.py` — кнопка `🌍 Global Presence` в Operations
   - `main.py` — роутер зарегистрирован
   - Удалено дублирование `_progress_text` в `mass_publish.py`

2. **Предыдущие сессии**:
   - Исправлен инвайтинг (ChatAdminRequiredError, access_hash, human delays)
   - Исправлен вход в аккаунты (ResendCodeRequest → fresh SendCodeRequest)
   - Отмена запущенных задач (asyncio.Task.cancel + CancelledError)
   - Device fingerprints для аккаунтов (schema_v23)
   - Behavioral Engine, Session Simulator, Alerts, Notifications, Visibility Reports

### 🔄 Текущая ветка
`claude/telegram-bot-services-xfAh6`
Last commit: `refactor: UX channel_ops + accounts`

### ✅ Выполнено в сессии 2026-05-29 (r6-r8)

**Критические исправления:**
- schema_v39.sql: полный backfill last_seen/registered_at из старых колонок (fix crash UndefinedColumnError)
- start.py: compat last_seen/last_active
- config.py: цены из env vars PRICE_STARTER/PRO/ENTERPRISE
- db.py: grant_plan + revoke_plan пишут в subscriptions table (get_plan читает subscriptions, не platform_users.current_plan)
- db.py: get_all_platform_users с COALESCE для обратной совместимости
- admin.py: правильный счётчик юзеров из platform_users, кнопки «Цены» и «Методы оплаты»
- subscription.py: /subscription сразу открывает меню биллинга

**Новые фичи:**
- schema_v40.sql: acc_status, status_checked_at, status_reason в tg_accounts
- account_manager.py: check_account_status_full (active/banned/spamblock/cooldown/session_expired + SpamBot check)
- accounts.py: статус-emoji ✅⏳⚠️❌💀🔑📦, фильтры (Все/Активные/Проблемные)
  - «🔍 Проверить все» — bulk check всех аккаунтов с обновлением БД
  - «🔎 Найти ресурсы» — scan_owned_assets по всем аккаунтам → импорт в managed_channels
  - ACC_LIMITS исправлены: free=2, starter=5, pro=15, enterprise=∞
- group_factory.py: subscription gates (create→PRO, announce→STARTER)

**UX-рефакторинг:**
- channel_ops.py: переструктурировано меню (без дублирования «Пост в каналы» vs «Опубликовать пост»)
  - manage_dialogs: сначала из БД (managed_channels), потом кнопка «Загрузить из Telegram»
  - manage_dialogs_live: scan_owned_assets (только admin/creator), сохраняет в managed_channels
  - username каналов видны прямо в списке

### 🔜 Следующие приоритеты

**P1:**
- [ ] Global Presence Factory — поддержка ботов + пакеты
- [ ] Bulk actions: настройки задержки, выбор аккаунтов, preview перед запуском
- [ ] AI Assistant: реальное выполнение команд (создание каналов/ботов/групп через BotMother API)

**P2:**
- [ ] UX cleanup: ещё много кнопок без ясного назначения → аудит всех меню
- [ ] CSV import для bulk operations
- [ ] Webhook для платежей (вместо polling)

### Проект
- Stack: aiogram 3.13.1, asyncpg, Telethon, Railway
- DB: 58+ таблиц (v40 schema), последняя схема v40
- Handlers: 45+ файлов
- Ветка: `claude/telegram-bot-services-xfAh6`
