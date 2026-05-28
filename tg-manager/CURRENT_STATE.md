# BotMother — Current State (v3.2)

## Last Updated

2026-05-28

## Repository Summary

Ветка: `claude/telegram-bot-services-xfAh6`
Build: `2026.05.27-r2`
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

## Existing Features (все работают)

- Multi-account management (QR/phone/session/import)
- Device fingerprints per account (schema_v23)
- Import channels/groups из Telegram
- Bot management, Channel Factory, Group Factory
- Cluster Manager, Proxy Manager, Health Dashboard
- Mass Ops, Operation Queue, Mass Publish (Smart Timing)
- Network Broadcast, Asset Templates (full apply)
- Channel Operations (join/leave/publish/edit)
- Search Rankings, Competitors, Visibility Reports [STARTER]
- Alerts system [FREE/STARTER]
- Behavioral Engine + Dashboard [PRO]
- Session Simulator (интегрирован)
- Relay, Auto-reply, CRM, Funnels, Schedules, Broadcast
- Subscription + Payment Checker + Referral System
- AI Assistant, Notifications Settings
- Account Monitor, Trust Engine, Shadowban Monitor
- Operation Reports [STARTER]

## Known Gaps (из gap-анализа)

### Критические (нет UI или нет доставки)
- Operation Planner FSM — нет UI (заглушка)
- Notification Delivery — настройки есть, отправка не реализована
- Post Template → Mass Publish auto-inject — redirect работает, prefill нет

### Средние (частично работают)
- Behavioral collectors не вызываются из handlers
- Operation Builder — очередь есть, FSM wizard неполный
- Experiment conversion не вызывается из auto_responder

### Низкий приоритет
- Visibility Report CSV export
- Search Memory drill-down
- Webhook для платежей
- Admin bulk tools

## Active Task Queue

Смотри `TASK_QUEUE.md` — 9 задач, приоритеты P0-P2.

## Next Safe Task

TASK-001: Operation Planner FSM
