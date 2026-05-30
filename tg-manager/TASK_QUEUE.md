# TASK QUEUE

Обновлено: 2026-05-30 (r15 → r16)

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
- [x] Inline help для сложных полей (5f4f73c)
- [x] Валидация input-данных перед сохранением во всех FSM — исправлено в auto_reply, funnels, schedule, deeplinks, broadcast

## P3 — Targeting and templates (ГОТОВО)
- [x] Add reusable target selection abstraction (b046eef)
- [x] Add template placeholder rendering (198da66)
- [x] Add template validation (0078c07)
- [x] Add drift/template compare — автоматическое сравнение с шаблонами (8ec458a)

## P4 — Global Presence Factory (ГОТОВО V1 + V2 + V3)
- [x] Add Global Presence menu entry
- [x] Implement guided flow: asset type → template → name → username → geo → accounts → preview → confirm
- [x] Add geo seed/preset system (5 пресетов: EU 44, World 51, Tier-1 50, DACH 20, LATAM 25)
- [x] Add username uniqueness/fallback engine (transliterate + slugify + variants)
- [x] Add account pool selection/distribution
- [x] Execute through Operation Engine (op_worker with safe pacing 45-90s)
- [x] Add progress, retry failed, report
- [x] Support for groups (V2: megagroup=True, f7719f0)
- [x] Поддержка ботов + пакеты (V3, e695b82)

## P5 — Advanced (ГОТОВО)
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
- [x] Import center — CSV батч-импорт аккаунтов + валидация сессий (6593c17)
- [x] Drift detection — мониторинг + алерты + сравнение с шаблонами (d934241, 8ec458a)
- [x] Health Dashboard sparklines — ASCII/Unicode графики (ae9d910)
- [x] Topology map (5a6cf53)

---

## 🟡 НИЗКИЙ ПРИОРИТЕТ (nice to have, r17+)

- [ ] Telegram Mini App для аналитики
- [ ] RBAC / Multi-user workspaces
- [ ] Approval workflows для критических bulk-операций

---

## ИТОГИ r15 → r16

Все приоритетные задачи r13-r15 выполнены.
Оставшиеся задачи — низкоприоритетные (nice to have).
Ожидаются новые указания пользователя для r16.
