# TASK QUEUE

Обновлено: 2026-05-30 (r14)

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
- [x] Bulk join/leave delay selector — 4 режима pacing (r13)
- [x] Bulk join/leave file upload — .txt список до 200 строк (r13)
- [x] Account preview в bulk_join/leave при выборе "все аккаунты" (r13)

## P2 — Telegram UX (ГОТОВО)
- [x] Добавлены Back-кнопки на все lock-screen экраны подписки (d005062)
- [x] Описания всех разделов BotMother OS меню
- [x] Онбординг с тремя сценариями для новых пользователей
- [x] Статус-иконки ✅/⛔ в списке аккаунтов
- [x] Описания в Channel Factory, Group Factory, Mass Publish
- [x] experiments.py FSM: кнопки Отмены на всех шагах (r13)
- [x] Полный аудит всех меню на button dumps — аудит проведён, button dumps не найдены
- [x] Проверка консистентности Cancel/Back во всех FSM — исправлено 6 файлов: auto_reply, funnels, schedule, deeplinks, asset_templates, broadcast
- [x] Inline help для сложных полей
- [x] Валидация input-данных перед сохранением во всех FSM — исправлено в auto_reply, funnels, schedule, deeplinks, broadcast

## P3 — Targeting and templates
- [x] Add reusable target selection abstraction
- [x] Add template placeholder rendering
- [x] Add template validation
- [x] Add drift/template compare plan (в drift_detector.py — автоматическое сравнение изменений с шаблонами)

## P4 — Global Presence Factory (ГОТОВО V1 + V2 + V3)
- [x] Add Global Presence menu entry (🌍 Global Presence в Operations)
- [x] Implement guided flow: asset type → template → name → username → geo → accounts → preview → confirm
- [x] Add geo seed/preset system (5 пресетов: EU 44, World 51, Tier-1 50, DACH 20, LATAM 25)
- [x] Add username uniqueness/fallback engine (transliterate + slugify + variants)
- [x] Add account pool selection/distribution
- [x] Execute through Operation Engine (op_worker with safe pacing 45-90s)
- [x] Add progress, retry failed, report
- [x] Support for groups (V2: megagroup=True, f7719f0)
- [x] Поддержка ботов + пакеты (V3, e695b82)

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
- [x] AI Assistant — реальное выполнение команд (r13, dcb90e6)
- [x] Bulk pacing — настройки темпа (r13, b1a351b, c1d8f5d)
- [x] Account Health Dashboard V2 — тренды, health_score, рекомендации (r14)
- [x] Auto-rotation аккаунтов — автоматические кулдауны (r14)
- [x] Behavioral Engine — velocity anomaly + pattern deviation (r14)
- [ ] Import center improvements (CSV import для bulk operations)
- [x] Drift detection foundation (d934241)
- [ ] Telegram Mini App для аналитики
- [ ] RBAC / Multi-user workspaces
- [ ] Approval workflows
- [x] Topology map (5a6cf53)

---

## ТЕКУЩИЕ ПРИОРИТЕТЫ (2026-05-30, r13)

### ✅ Выполнено (этот спринт, r13)
1. **AI Assistant — реальное выполнение команд**
   - [x] create_channel/bot/group/post_to_channel — все реализованы в ai_tools.py
   - [x] Confirmation flow: pending_action → confirm_action
   - [x] Интеграция с Operation Engine (bulk_create_channels → op_queue)

2. **Bulk actions — улучшение UX**
   - [x] Настройки задержки: 4 режима (fast/normal/slow/smart) для bulk_join и bulk_leave
   - [x] Выбор аккаунтов с превью (показывает первые 5 при выборе "все")
   - [x] File upload (.txt) для bulk_join и bulk_leave
   - [x] Кнопка "◀️ Изменить задержку" (bj_redelay/bl_redelay)

3. **UX cleanup — полный аудит**
   - [x] experiments.py: добавлены ❌ Отмена на всех шагах CreateExperiment FSM
   - [x] dm_campaigns.py, audience_parser.py, account_warmup.py — OK (нет проблем)
   - [ ] Осталось: bulk_chan_uname/about — выполняются сразу без preview/delay

### ⚠️ High (следующие недели)
4. **Global Presence Factory V3**
   - [ ] Поддержка создания ботов в каждом городе
   - [ ] Пакетное создание (каналы + группы + боты)
   - [ ] CSV import для своего списка городов

5. **Behavioral Engine Enhancement**
   - [ ] Fine-tune формулы scoring'а
   - [ ] Уникальные паттерны поведения для разных аккаунтов
   - [ ] Anomaly detection (алерты при отклонении от нормы)

6. **Account Health Dashboard V2**
   - [x] Тренды (cb_trust_trend) — реализовано
   - [x] Рекомендации (cb_health_recommendations) — реализовано
   - [x] Automatic rotation (cb_auto_rotate) — реализовано
   - [ ] Визуальные графики через ASCII/Unicode

### 📋 Medium (месяц)
7. **Import Center**
   - [x] File upload (.txt) для bulk_join/leave — СДЕЛАНО
   - [ ] Массовый импорт аккаунтов из CSV батчами
   - [ ] Валидация перед импортом (проверка session strings)

9. **Drift Detection**
   - [ ] Мониторинг изменений в каналах/ботах
   - [ ] Алерты при неожиданных изменениях
   - [ ] Сравнение с шаблонами

10. **UX cleanup — завершение**
    - [ ] Полный аудит ВСЕХ меню на button dumps
    - [ ] Cancel/Help консистентность
    - [x] Inline help для сложных полей
