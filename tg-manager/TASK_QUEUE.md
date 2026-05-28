# TASK QUEUE

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

## P2 — Telegram UX
- [ ] Review menus for button dumps
- [ ] Add Back/Cancel/Help consistency
- [ ] Add clear explanations to mass-action flows
- [ ] Ensure risky actions require confirmation

## P3 — Targeting and templates
- [ ] Add reusable target selection abstraction
- [ ] Add template placeholder rendering
- [ ] Add template validation
- [ ] Add drift/template compare plan if existing structures support it

## P4 — Global Presence Factory
- [ ] Add Global Presence menu entry
- [ ] Implement guided flow: asset type → template → name → username → geo → accounts → preview → confirm
- [ ] Add geo seed/preset system or integrate existing geo data
- [ ] Add username uniqueness/fallback engine
- [ ] Add account pool selection/distribution
- [ ] Execute through Operation Engine
- [ ] Add progress, retry failed, report
- [ ] Document v1/v2 scope clearly

## P5 — Advanced
- [ ] Visibility tracking foundation
- [ ] Drift detection foundation
- [ ] Import center improvements
- [ ] Security/billing/referral hardening

## ТЕКУЩИЕ ПРИОРИТЕТЫ (2026-05-28)

### 🔥 Urgent (этот спринт)
1. **Global Presence Factory V2** — поддержка групп и ботов
   - [ ] Вариант для создания групп (аналог channels)
   - [ ] Интеграция с group_factory FSM
   - [ ] CSV import для списка городов

2. **Reliability & Robustness**
   - [ ] Тестирование массовых операций на стабильность
   - [ ] Улучшение обработки ошибок в channel_ops
   - [ ] Добавление retry-логики для failed операций

3. **UX Improvements**
   - [ ] Описания для всех FSM-шагов
   - [ ] Inline help для сложных полей
   - [ ] Валидация input-данных перед сохранением

### ⚠️ High (недель 2-3)
4. **Search Memory Enhancement**
   - [ ] Drill-down по keyword → история позиций
   - [ ] Affinity score расчёт с историческими данными
   - [ ] Export в CSV

5. **Operation Reports**
   - [ ] Полная статистика по выполненным операциям
   - [ ] Детальный лог каждой операции
   - [ ] Проблемы и ошибки

6. **Account Health**
   - [ ] Расширенный health_dashboard с трендами
   - [ ] Рекомендации для восстановления trust_score
   - [ ] Automatic rotation при низком score

### 📋 Medium (месяц)
7. **Behavioral Engine Enhancement**
   - [ ] Fine-tune формулы scoring'а
   - [ ] Уникальные паттерны поведения
   - [ ] Anomaly detection

8. **Experiments & A/B**
   - [ ] Конверсия в фаннели и эксперименты
   - [ ] Статистическая значимость
   - [ ] Auto-pause low-performing experiments
