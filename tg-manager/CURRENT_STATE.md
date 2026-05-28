# CURRENT STATE

Обновлено: 2026-05-28 (r5)

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
Last commit: `feat: Global Presence Factory — создание Telegram-присутствия по всему миру`

### 🔜 Следующие приоритеты

**P0 — ГОТОВО** ✅
- [x] Operation Planner FSM — полная реализация с datetime-парсингом
- [x] Notification Delivery — UI в Settings + вызовы в account_monitor/ranking_checker
- [x] Post Template → Mass Publish auto-prefill — работает через tpl_prefill
- [x] Behavioral collectors — record_reentry в start.py, record_cross_nav в botmother_menu.py

**P1 — ГОТОВО** ✅
- [x] Global Presence Factory V2 — поддержка ГРУПП (f7719f0)
- [x] Operation Builder FSM — полная реализация с 4 типами операций
- [x] Operation Reports — UI + новые функции статистики (027cf95)
- [x] Search Memory drill-down — из behavioral_engine

**P2 — Выполнено в сессии 2026-05-28 (r4)**
- [x] trust_engine: исправлен критический баг `created_at` → `added_at`
- [x] ranking_checker: исправлен `MANAGER_BOT_TOKEN` → `notify_if_enabled(bot)`
- [x] schema_v36.sql: таблица account_trust_history (30-дневная история trust scores)
- [x] health_dashboard: кнопка 📈 Тренд + cb_trust_trend с 7-дневной историей
- [x] op_reports: сводная статистика (success rate, avg duration, counts)
- [x] new_user уведомление: auto_responder → notify_if_enabled при is_new_user=True
- [x] Behavioral dashboard: реальные имена (bot/channel/keyword) вместо #id
- [x] Alerts: реальные имена аккаунтов/ботов вместо acc#id/bot#id
- [x] op_worker: inline-кнопка «Детали операции» в уведомлениях done/failed
- [x] notify_if_enabled: добавлен параметр reply_markup

**P3 — Следующие приоритеты**
- [ ] Global Presence Factory V2 — поддержка БОТОВ + пакеты
- [ ] CSV import для списков городов/целей
- [ ] UX improvements: описания для всех FSM-шагов
- [ ] Улучшения reliability: retry-логика для failed операций

### Проект
- Stack: aiogram 3.13.1, asyncpg, Telethon, Railway
- DB: 57+ таблиц, последняя схема v36
- Handlers: 45+ файлов
- Ветка: `claude/telegram-bot-services-xfAh6`
