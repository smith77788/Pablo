# CURRENT STATE

Обновлено: 2026-05-28

## Статус: АКТИВНАЯ РАЗРАБОТКА

### ✅ Выполнено в текущей сессии

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

**P0 — Высокий приоритет**:
1. Operation Planner FSM — UI для scheduled_for в operation_queue (сейчас заглушка)
2. Notification Delivery — реальная отправка уведомлений через notification_settings
3. Post Template → Mass Publish auto-prefill
4. Behavioral collectors — record_reentry в start.py, record_cross_nav в nav

**P1 — Средний**:
5. Global Presence Factory V2 (группы/боты/пакеты, полная гео-база, CSV import)
6. Operation Builder FSM — полноценный wizard
7. Visibility Report CSV export
8. Search Memory drill-down

### Проект
- Stack: aiogram 3.13.1, asyncpg, Telethon, Railway
- DB: 57+ таблиц, последняя схема v35
- Handlers: 45+ файлов
- Ветка: `claude/telegram-bot-services-xfAh6`
