# CURRENT STATE

Обновлено: 2026-05-30 (r13)

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

### ✅ Выполнено в сессии 2026-05-30 (r12 → r13)

1. **Bulk actions pacing selector** — выбор задержки в bulk_join и bulk_leave wizard (59aaac1)
   - 4 режима: ⚡ Быстро (5-15с) / 🛡 Нормально / 🐌 Медленно / 🧠 Умный (авто)
   - op_worker читает delay_mode из params и применяет его
   - Кнопка ◀️ Изменить задержку + новые handlers bj_redelay/bl_redelay (477f0fe)
2. **Fix cancel buttons in experiments.py FSM** — все шаги CreateExperiment теперь имеют ❌ Отмена (078286f)
3. **File upload in bulk_join/bulk_leave** — загрузка .txt списка до 200 строк (eb88a80)
4. **Account preview at step 3/4** — при выборе "все аккаунты" показывает первые 5 имён (4b816f6)

### ✅ Выполнено в сессии 2026-05-30 (r11 → r12)

1. **Кнопка Релог** — переподключение аккаунта без повторного ввода номера (4479677)
2. **Resilient service restart** — factory pattern вместо coroutine reuse (3c50b7f)
   - ⚠️ ВСЕ 15 сервисов тихо не перезапускались после краша — теперь исправлено
3. **Background mass_publish** — регистрация в task_registry (32d2946)
4. **Telethon operation timeouts** — `_OP_TIMEOUT=45s`, wait_for в iter_dialogs/get_me (30065ce)
5. **Active Tasks button** — кнопка ⚡ Активные задачи в главном меню + /tasks (f5119f7)
6. **DM campaign task registration** — backgrounded + CancelledError → status=paused (9adee3c)
7. **Live task tracking** — services/task_registry.py + active_tasks handler (e6cfd05)
   - Strike, mass_publish, dm_campaign, invite — все backgrounded + отменяемы через /tasks
8. **SQL-инъекции устранены** (d6e2018)
   - `admin.py`: key из callback data → параметр `$2` (не f-string)
   - `op_worker.py`: backoff в f-string → `$4 * interval '1 second'`
9. **Silent fails исправлены** (d6e2018)
   - 5 функций db.py с `except Exception: pass` → `log.debug(...)` — ошибки теперь видны в логах

### ✅ Выполнено в предыдущих сессиях (r6-r11)

**Strike Module & Enterprise (r11):**
- Strike module: 12-векторная атака + disclaimer + $250 lifetime (47f7faa)
- Enterprise-only tier: продвинутые фичи + self-healing schema loader (39d33c1)
- Subscription tier redesign: исправление strike_access + переработка тиров (53a748b)
- Payment plan=strike обработка (fc6b418)
- Report peer deep: 8-векторная атака v2 (89a07dd, bfa1355)
- Многоязычные тексты жалоб: 10 языков × 6 типов (8f998cc)
- Bulk report fix: KeyError session_str (8d3351f)
- Admin Strike grant UI (8774898)

**Operation Builder & UX (r9-r10):**
- Operation Builder FSM wizard в mass_ops (58e0be4, d005062)
- Back buttons на все lock-screen экраны (d005062)
- Visibility Report CSV export + Search Memory drill-down (519f357)
- Bulk report с выбором аккаунтов (checkbox UI + прогресс) (cc59261)
- Notification на авто-завершение A/B эксперимента (01b4c77)
- Fix: рабочая кнопка Назад на всех lock-screen экранах (0a6ea55)
- Fix: кнопка Назад использует managed_channels (b9462b2)
- Авто-завершение A/B экспериментов по статистической значимости (fa27f07)

**Критические исправления (r6-r8):**
- schema_v39.sql: полный backfill last_seen/registered_at (fix UndefinedColumnError)
- start.py: compat last_seen/last_active
- config.py: цены из env vars PRICE_STARTER/PRO/ENTERPRISE
- db.py: grant_plan + revoke_plan пишут в subscriptions table
- db.py: get_all_platform_users с COALESCE для обратной совместимости
- admin.py: правильный счётчик юзеров, кнопки «Цены» и «Методы оплаты»
- subscription.py: /subscription сразу открывает меню биллинга

**Infrastructure OS Layer (r10):**
- docs/COMPETITOR_GAP_ANALYSIS_TELE_RAPTOR.md: анализ разрыва
- services/flood_engine.py: Flood Intelligence Engine
- services/session_pool.py: Session Orchestrator
- services/account_health.py: Account Health Engine
- services/parser.py: Audience Parser
- services/account_warmer.py: Account Warming
- schema_v41.sql: 7 новых таблиц для инфраструктуры
- bot/handlers/audience_parser.py: UI парсера
- bot/handlers/account_warmup.py: UI разогрева
- bot/handlers/proxy_manager.py: Proxy Intelligence
- bot/handlers/seo.py: CRITICAL FIX — текстовый фидбек + username

**Bulk Channel Operations (r9):**
- channel_ops.py: bulk_chan_uname + bulk_chan_about
- FSM: BulkChanFSM.waiting_value → валидация → прогресс → отчёт
- Авто-обновление DB cache (managed_channels.username)

**Global Presence Factory V1 + V2:**
- V1: Полный FSM wizard 8 шагов (schema_v35, geo_data, username_engine, presence_planner)
- V2: Поддержка групп (f7719f0), megagroup=True/False
- 5 гео-пресетов (EU 44, World 51, Tier-1 50, DACH 20, LATAM 25)
- Выполнение через op_worker с safe pacing 45-90s

**Operation Reports Enhancement:**
- get_operation_stats(), get_user_operation_history(), count_operation_errors()
- Operation Reports UI в botmother_menu.py
- Детальный анализ ошибок и производительности

### 🔄 Текущая ветка
`claude/telegram-bot-services-xfAh6`
Last commit: `4b816f6 feat: превью аккаунтов в bulk_join/bulk_leave`

### 🔜 Следующие приоритеты (2026-05-30)

**P1 — Этот спринт:**
- [x] AI Assistant: реальное выполнение команд (create_channel/bot/group/post — уже реализовано)
- [x] Bulk actions: настройки задержки, выбор аккаунтов, preview перед запуском
- [x] Полный UX-аудит всех меню — критические fixes сделаны (experiments.py cancel buttons)
- [x] File upload для bulk_join/bulk_leave (.txt со списком)

**P2 — Следующий спринт:**
- [ ] Global Presence Factory V3: поддержка ботов + CSV импорт городов
- [x] Account Health Dashboard V2: тренды, рекомендации, auto-rotation — РЕАЛИЗОВАНО
- [ ] Behavioral Engine Enhancement: fine-tune + anomaly detection

**P3 — Бэклог:**
- [x] Import Center файловый импорт (.txt для bulk_join/leave добавлен)
- [ ] Import Center: CSV импорт аккаунтов батчами
- [ ] Drift Detection (мониторинг изменений, алерты)
- [ ] Telegram Mini App для аналитики
- [ ] Topology map (граф связей)

### Проект
- Stack: aiogram 3.13.1, asyncpg, Telethon, Railway
- DB: 60+ таблиц (v44 schema), последняя схема v44
- Handlers: 47+ файлов
- Services: 20+ фоновых сервисов
- Ветка: `claude/telegram-bot-services-xfAh6`
- Build: `2026.05.30-r12`
