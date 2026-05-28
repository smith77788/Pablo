# BotMother — Current State (v3.2)

## Last Updated

2026-05-28 (after full TASK_QUEUE completion)

## Repository Summary

Ветка: `claude/telegram-bot-services-xfAh6`
Build: `2026.05.28-r1`
Deploy: Railway, auto-deploy при пуше
AI Memory: v3.2 (autonomous loop + task queue)

## Current Stack

| Component | Version |
|-----------|---------|
| aiogram | 3.13.1 + Pydantic v2 |
| asyncpg | PostgreSQL driver |
| Telethon | userbot ops |
| Python | 3.11+ |
| DB Schema | v33 (57+ таблиц) |

## Existing Bot Architecture

- 44+ handler файлов в `bot/handlers/`
- 46 уникальных CallbackData префиксов в `bot/callbacks.py`
- FSM состояния в `bot/states.py`
- 12 фоновых сервисов зарегистрированы в `main.py`
- Subscription gates на 28+ фичах (4 тира: free/starter/pro/enterprise)

## Completed Task Queue (все выполнены)

| Task | Status | Описание |
|------|--------|----------|
| TASK-001 | DONE | Operation Planner FSM wizard |
| TASK-002 | DONE | Notification Delivery (notify_if_enabled) |
| TASK-003 | DONE | Post Template → Mass Publish auto-inject |
| TASK-004 | DONE | Behavioral Collectors Wiring |
| TASK-005 | DONE | Operation Builder FSM Wizard + bulk_leave |
| TASK-006 | DONE | Experiment Conversion Tracking |
| TASK-007 | DONE | Visibility Report CSV Export |
| TASK-008 | DONE | Search Memory Drill-Down |
| TASK-009 | DONE | Docs update |

## Existing Features (все работают)

### Инфраструктура
- Multi-account management (QR/phone/session/import)
- Device fingerprints per account (schema_v23)
- Import channels/groups из Telegram
- Bot management, Channel Factory, Group Factory
- Cluster Manager, Proxy Manager, Health Dashboard

### Операции
- **Operation Builder** — 4 типа: mass_publish, bulk_join, bulk_leave, bulk_bot_edit
- **Operation Queue** — просмотр, прогресс, отмена
- **Operation Planner** — планирование операций на конкретное время (scheduled_for)
- **Operation Reports** [STARTER] — история выполненных операций
- Mass Publish (Smart Timing 30-90s, dry-run, по аккаунту/кластеру)
- Network Broadcast, Asset Templates (full apply с prefill)
- Channel Operations (join/leave/publish/edit)

### Видимость
- Search Rankings (трекинг позиций)
- Competitors (мониторинг конкурентов)
- Visibility Reports [STARTER] + CSV export
- Alerts system [FREE/STARTER]
- **Search Memory Drill-Down** — история позиций по keyword из behavioral dashboard

### Поведенческий слой
- Behavioral Events log (reentry, cross_nav записываются)
- Behavioral Engine (attention/habit/ecosystem/decay каждые 15 мин)
- Search Memory (keyword affinity, записывается при поиске)
- Behavioral Dashboard [PRO] с drill-down по keywords

### Коммуникация
- Relay (входящие диалоги, ответы)
- Auto-reply (правила, A/B эксперименты с conversion tracking)
- CRM, Funnels, Schedules, Broadcast

### Монетизация и настройки
- Subscription (4 тира + gates)
- Payment Checker, Referral System
- AI Assistant
- Notifications Settings (per-user toggle) — delivery реализована через notify_if_enabled

### Мониторинг
- Account Monitor → notify_if_enabled(flood_warning/restriction)
- Trust Engine, Shadowban Monitor
- Op Worker → notify_if_enabled(op_complete)

## Known Remaining Gaps

### Низкий приоритет (не в текущей очереди)
- Webhook для платежей (сейчас polling)
- Admin bulk tools
- Telegram Mini App для аналитики
- RBAC / Multi-user workspaces
- Topology map (граф связей)

## Active Task Queue

Смотри `TASK_QUEUE.md` — все 9 задач DONE. Очередь пуста.

## Next Safe Task

Нет задач в очереди. Для новых задач — добавить в TASK_QUEUE.md.
