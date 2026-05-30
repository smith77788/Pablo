# TASK QUEUE

Обновлено: 2026-05-30 (r12)

## P0 — CRITICAL: BotMother OS consolidation
- [x] **IMMEDIATELY**: Убрать прямые команды: `/ai`, `/accounts`, `/ops`, `/ranking`, `/referral`, `/subscription`
- [x] Все функции ДОЛЖНЫ входиться только через BotMother OS меню
- [x] Дока: ARCHITECTURE_ISSUES.md (убедиться что все понимают проблему)
- [x] Заменить на redirect: `/ai` → "Откройте BotMother → 🤖 AI Assistant"
- [x] Коммит 9ddf27f + 549a339 (deploy trigger с версией r3)

## P0 — Repository understanding (СДЕЛАНО)
- [x] Inspect repository structure
- [x] Detect stack, bot framework, database, queues/workers
- [x] Update docs/ARCHITECTURE.md
- [x] Update docs/FEATURE_INVENTORY.md
- [x] Update docs/GAP_ANALYSIS.md
- [x] Update docs/ROADMAP.md

## P1 — Foundation (ГОТОВО)
- [x] Operation Planner FSM — полная реализация с datetime-парсингом
- [x] Notification Delivery — UI в Settings + вызовы в account_monitor/ranking_checker
- [x] Post Template → Mass Publish auto-prefill — работает через tpl_prefill
- [x] Behavioral collectors — record_reentry в start.py, record_cross_nav в botmother_menu.py
- [x] Preview/confirmation для массовых операций (dry_run в mass_ops)
- [x] Operation Builder FSM — полная реализация с 4 типами операций
- [x] Live task tracking and cancellation system (e6cfd05)
- [x] Active Tasks button in main menu + /tasks keyboard (f5119f7)
- [x] DM campaign task registration + cancellation propagation (9adee3c)
- [x] Background mass_publish + task_registry (32d2946)
- [x] Telethon operation timeouts (30065ce)
- [x] Resilient service restart — factory pattern (3c50b7f)
- [x] Relog button — reconnect account without re-entering phone (4479677)
- [x] SEO AI fix — принимает текстовый фидбек, спрашивает username

## P2 — Telegram UX (ЧАСТИЧНО)
- [x] Добавлены Back-кнопки на все lock-screen экраны подписки (d005062)
- [x] Описания всех разделов BotMother OS меню
- [x] Онбординг с тремя сценариями для новых пользователей
- [x] Статус-иконки ✅/⛔ в списке аккаунтов
- [x] Описания в Channel Factory, Group Factory, Mass Publish
- [ ] Полный аудит всех меню на button dumps
- [ ] Проверка консистентности Cancel/Help во всех FSM
- [ ] Inline help для сложных полей
- [ ] Валидация input-данных перед сохранением во всех FSM

## P3 — Targeting and templates
- [ ] Add reusable target selection abstraction
- [ ] Add template placeholder rendering
- [ ] Add template validation
- [ ] Add drift/template compare plan if existing structures support it

## P4 — Global Presence Factory (ГОТОВО V1 + V2)
- [x] Add Global Presence menu entry (🌍 Global Presence в Operations)
- [x] Implement guided flow: asset type → template → name → username → geo → accounts → preview → confirm
- [x] Add geo seed/preset system (5 пресетов: EU 44, World 51, Tier-1 50, DACH 20, LATAM 25)
- [x] Add username uniqueness/fallback engine (transliterate + slugify + variants)
- [x] Add account pool selection/distribution
- [x] Execute through Operation Engine (op_worker with safe pacing 45-90s)
- [x] Add progress, retry failed, report
- [x] Support for groups (V2: megagroup=True, f7719f0)
- [ ] Поддержка ботов + пакеты (V3)

## P5 — Advanced (ЧАСТИЧНО)
- [x] Visibility Reports CSV export (519f357)
- [x] Search Memory keyword drill-down (519f357)
- [x] Operation Reports — полная статистика + детальный лог (027cf95)
- [x] Account Health Engine (services/account_health.py)
- [x] Flood Intelligence Engine (services/flood_engine.py)
- [x] Session Orchestrator (services/session_pool.py)
- [x] Audience Parser (services/parser.py + handler)
- [x] Account Warming (services/account_warmer.py + handler)
- [x] Proxy Intelligence — latency, geo, scoring
- [x] A/B эксперименты — авто-завершение по статистической значимости (fa27f07)
- [x] Strike Module — 12-векторная атака + disclaimer (47f7faa)
- [x] Enterprise tier — все продвинутые фичи + self-healing schema loader (39d33c1)
- [x] Session Converter (services/session_converter.py)
- [x] Account Cleaner (services/account_cleaner.py + handler)
- [x] Bulk Channel Operations — массовый username/about (r9)
- [x] Payment Webhook (services/payment_webhook.py, port 8080)
- [x] Admin bulk tools (grant + cleanup + platform ops)
- [ ] Import center improvements (CSV import для bulk operations)
- [ ] Drift detection foundation
- [ ] Telegram Mini App для аналитики
- [ ] RBAC / Multi-user workspaces
- [ ] Approval workflows
- [ ] Topology map

---

## ТЕКУЩИЕ ПРИОРИТЕТЫ (2026-05-30, r12)

### 🔥 Urgent (этот спринт)
1. **AI Assistant — реальное выполнение команд**
   - [ ] Создание каналов/ботов/групп через AI → BotMother API
   - [ ] Интеграция AI с Operation Engine
   - [ ] Подтверждение перед выполнением

2. **Bulk actions — улучшение UX**
   - [ ] Настройки задержки (кастомный pacing)
   - [ ] Выбор аккаунтов с превью
   - [ ] Preview перед запуском массовых операций

3. **UX cleanup — полный аудит**
   - [ ] Проверить ВСЕ меню на button dumps
   - [ ] Убедиться что Back/Cancel есть везде
   - [ ] Добавить описания где пропущены

### ⚠️ High (недель 2-3)
4. **Global Presence Factory V3**
   - [ ] Поддержка создания ботов
   - [ ] Пакетное создание (каналы + группы + боты в каждом городе)
   - [ ] CSV import для списка городов

5. **Behavioral Engine Enhancement**
   - [ ] Fine-tune формулы scoring'а
   - [ ] Уникальные паттерны поведения
   - [ ] Anomaly detection

6. **Account Health Dashboard V2**
   - [ ] Тренды (графики изменения health_score)
   - [ ] Рекомендации для восстановления trust_score
   - [ ] Automatic rotation при низком score

### 📋 Medium (месяц)
7. **Import Center**
   - [ ] CSV import для bulk операций
   - [ ] Массовый импорт аккаунтов из файла
   - [ ] Валидация перед импортом

8. **Drift Detection**
   - [ ] Мониторинг изменений в каналах/ботах
   - [ ] Алерты при неожиданных изменениях
   - [ ] Сравнение с шаблонами

9. **Web UI / Telegram Mini App**
   - [ ] Дашборд для больших таблиц/графиков
   - [ ] Топологические карты
   - [ ] Расширенные фильтры
