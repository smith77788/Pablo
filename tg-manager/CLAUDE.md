# CLAUDE.md — Главный ориентир BotMother OS

> Этот файл читается автоматически при каждой сессии Claude Code.
> Он содержит всё необходимое для продолжения работы без потери контекста.

---

## 1. ПРАВИЛА (всегда соблюдать)

```
1. Отвечать ТОЛЬКО на русском языке
2. Все изменения ТОЛЬКО в папке tg-manager/
3. НЕ удалять и НЕ ломать существующую функциональность
4. sessionEncrypted НИКОГДА не возвращать в API-ответах
5. Bot tokens — только в зашифрованном виде, никогда в логах
6. Никаких спам/обход/эвазия/абьюз workflows
7. Перед коммитом — python3 -m py_compile каждого изменённого файла
8. Ветка разработки: claude/telegram-bot-services-xfAh6
9. git push после каждого коммита (stop-hook это требует)
```

---

## 2. КОНЦЕПЦИЯ СИСТЕМЫ — BotMother OS

**Идея:** Telegram-native операционная система для управления Telegram-активами как бизнесом.

Пользователь управляет **экосистемой**: аккаунты → боты → каналы → группы → аудитория → монетизация.
Система понимает поведенческую физику Telegram 2026: внимание угасает, привычки формируются, ботов блокируют за предсказуемость.

### Три уровня системы

```
ИНФРАСТРУКТУРА        ИНТЕЛЛЕКТ              МОНЕТИЗАЦИЯ
─────────────────     ──────────────────     ──────────────────
Аккаунты              Behavioral Engine      Подписки (4 tier)
Боты                  Search Rankings        Реферальная сеть
Каналы                Trust Scores           Payment Checker
Группы                Shadowban Monitor      Operation Reports
Кластеры              AI Assistant           CRM + Funnels
Прокси                Cohort Analytics       Auto-reply
```

---

## 3. ТЕХНИЧЕСКАЯ АРХИТЕКТУРА

### Стек
- **Framework:** aiogram 3.13.1 + Pydantic v2
- **DB:** PostgreSQL через asyncpg (Railway)
- **Telegram API:** Telethon (userbot) + Bot API
- **Deploy:** Railway, Root Directory = `/tg-manager`, auto-deploy при пуше в ветку

### Ключевые файлы
```
tg-manager/
├── main.py                      # точка входа, регистрация роутеров + сервисов
├── config.py                    # BOT_TOKEN, DB_URL, ADMIN_IDS
├── database/
│   └── db.py                    # 163 функции, create_pool() авто-мигрирует schema_v*.sql
├── bot/
│   ├── callbacks.py             # ВСЕ CallbackData классы — только здесь
│   ├── states.py                # ВСЕ FSMState классы — только здесь
│   ├── keyboards.py             # subscription_locked_markup и др. shared KB
│   ├── handlers/                # 44 файла обработчиков
│   │   └── botmother_menu.py    # главное меню OS — точка входа /menu
│   └── utils/
│       ├── op_helpers.py        # _acc_label, _get_active_accounts, _progress_bar, _progress_text, _format_duration
│       └── subscription.py     # require_plan(), get_plan(), locked_text()
└── services/                    # 19 фоновых/утилитарных модулей
    ├── account_manager.py       # ВСЕ Telethon-операции
    ├── behavioral_engine.py     # поведенческие оценки (каждые 15 мин)
    └── session_simulator.py     # human-like delays
```

### Авто-миграция БД
`create_pool()` автоматически выполняет все `schema_v*.sql` в порядке версии.
Новая схема → новый файл `schema_v{N+1}.sql`. Последняя версия: **v32**.

---

## 4. КРИТИЧЕСКИЕ ПАТТЕРНЫ КОДА

### 4.1 CallbackData — Pydantic v2 (ОБЯЗАТЕЛЬНО)
```python
# ПРАВИЛЬНО — Optional[str] для полей которые могут быть пустыми
class BmCb(CallbackData, prefix="bm"):
    action: str
    sub: Optional[str] = None   # НЕ str = ""
    page: int = 0

# НЕПРАВИЛЬНО — вызывает ValidationError при десериализации пустых сегментов
class BmCb(CallbackData, prefix="bm"):
    sub: str = ""               # СЛОМАЕТ кнопки в aiogram 3.13
```

### 4.2 Subscription Gate — стандартный паттерн
```python
@router.callback_query(SomeCb.filter(F.action == "feature"))
async def cb_feature(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Название фичи", "starter"),
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    await callback.answer()
    # ... логика
```

### 4.3 Telethon клиент — всегда передавать _acc
```python
# ПРАВИЛЬНО — передать _acc для device fingerprint
result = await account_manager.some_operation(
    acc["session_str"],
    arg1, arg2,
    _acc=acc,                   # dict с полями acc
)

# НЕПРАВИЛЬНО — создаёт клиент без device fingerprint (риск бана)
result = await account_manager.some_operation(acc["session_str"], arg1)
```

### 4.4 Общие хелперы — только из op_helpers
```python
# ПРАВИЛЬНО
from bot.utils.op_helpers import _acc_label, _get_active_accounts, _progress_bar, _progress_text, _format_duration

# НЕПРАВИЛЬНО — НЕ определять эти функции локально в handler-файлах
def _acc_label(acc): ...       # ДУБЛИКАТ — удалить
def _progress_bar(...): ...    # ДУБЛИКАТ — удалить
```

### 4.5 FSM Template Prefill
```python
# Применение шаблона в asset_templates.py: сохранить prefill в state
await state.update_data(tpl_prefill={"title": "...", "description": "...", "username": "..."})
await state.set_state(ChannelFactoryFSM.choosing_account)

# В account_chosen handler: проверить prefill и пропустить FSM-шаги
sd = await state.get_data()
prefill = sd.get("tpl_prefill") or {}
if prefill.get("title"):
    await state.update_data(title=prefill["title"], about=prefill.get("description",""), tpl_prefill=None)
    await _show_chanf_cluster_or_confirm(callback, state, pool)
    return
```

### 4.6 Bulk-операции — anti-ban delays
```python
# channel_ops.py паттерн — экспоненциальный backoff + human delay
await asyncio.sleep(max(_backoff(attempt, base=2.0, cap=30.0), flood, _human_delay(25, 40)))

# services/session_simulator.py — для новых bulk-операций
from services.session_simulator import human_delay, bulk_item_pause
await bulk_item_pause(index, batch_size=10)   # длинная пауза каждые 10 элементов
await human_delay(1.5, 8.0)                   # beta-распределение, не равномерное
```

---

## 5. ВСЕ CALLBACK-ПРЕФИКСЫ (46 штук, все уникальны)

```
bot  edit  aud  wh   bc   bulk  cmd  tpl  sch  mg
ar   rl    fn   st   note sw    crm  au   exp  dl
eng  seo   net  cl   sub  ai    nbc  acc  rank chan
cinv ref   bm   atpl grpf mop   btf  chanf mpub comp
vis  hlth  prx  clm
```

**Правило:** новый CallbackData — новый уникальный prefix в `bot/callbacks.py`.

---

## 6. ЗАРЕГИСТРИРОВАННЫЕ РОУТЕРЫ В main.py (44 штуки)

Порядок регистрации важен — более специфичные роутеры раньше:
1. `bm_handler` (botmother_menu) — первый, т.к. самый общий
2. Factory handlers: bot_factory, group_factory, chan_factory
3. Operation handlers: mass_ops, asset_tpl, mass_pub, competitors
4. Sub/billing: sub_handler
5. Core: start, bots, edit, audience, webhooks, broadcast, etc.
6. `relay_handler` — последний среди роутеров (ловит F.reply_to_message)
7. `admin_handler` — самый последний

---

## 7. ФОНОВЫЕ СЕРВИСЫ (12 штук в main.py)

```python
asyncio.create_task(scheduler.run(pool, http))
asyncio.create_task(auto_responder.run(pool, http))
asyncio.create_task(relay_service.run(pool, http))
asyncio.create_task(funnel_runner.run(pool, http))
asyncio.create_task(payment_checker.run(pool, http, bot))
asyncio.create_task(ranking_checker.run(pool, bot))
asyncio.create_task(search_observer.run_confirmation_loop(pool, bot))
asyncio.create_task(account_monitor.run(pool, bot))
asyncio.create_task(trust_engine.run(pool))
asyncio.create_task(shadowban_monitor.run(pool, bot))
asyncio.create_task(op_worker.run(pool, bot))
asyncio.create_task(behavioral_engine.run(pool))   # NEW — каждые 15 мин
```

---

## 8. АНТИ-БАН СИСТЕМА (полностью реализована)

### Device Fingerprints
- 20 уникальных Android-профилей в `services/account_manager.py:_ANDROID_DEVICES`
- Каждый аккаунт имеет `device_model`, `system_version`, `app_version` в БД (schema_v23)
- `generate_device_fingerprint()` → рандомный профиль при создании аккаунта
- `_make_client(session_str, _acc)` → использует сохранённый профиль аккаунта

### Flood Protection (channel_ops.py)
- Exponential backoff: `_backoff(attempt, base=2.0, cap=60.0)`
- Flood wait respect: `asyncio.sleep(max(backoff, flood_seconds))`
- Human delay между операциями: `_human_delay(25, 40)` секунд
- Batch паузы: длинные паузы каждые N операций

### Account Trust System
- `trust_engine` фоновый сервис обновляет `trust_score` каждого аккаунта
- `account_monitor` следит за ограничениями (dm_restricted, search_drop, invite_degraded)
- `shadowban_monitor` детектирует теневые баны через `restriction_events`
- Операции используют аккаунты с наивысшим `trust_score` (ORDER BY trust_score DESC NULLS LAST)

### Session Realism (services/session_simulator.py)
```python
human_delay(1.5, 8.0)    # beta-распределение — не равномерное
short_pause(0.3, 1.5)    # быстрая пауза между лёгкими действиями
typing_delay(text)        # симуляция набора текста
bulk_item_pause(i, 10)   # длинная пауза каждые 10 элементов
chaos_factor()            # мультипликатор 0.7–1.3 для рандомизации тайминга
```

### Behavioral Engine (services/behavioral_engine.py)
Собирает поведенческие события → вычисляет:
- `attention_score` — устойчивость внимания (0–100)
- `habit_score` — привычность (0–100, регулярность по неделям)
- `ecosystem_score` — встроенность в экосистему (cross-nav links)
- `decay_rate` — скорость угасания (per day)

---

## 9. СХЕМА БД — КЛЮЧЕВЫЕ ТАБЛИЦЫ (57 таблиц, 32 схемы)

```sql
-- Аккаунты (с device fingerprint)
tg_accounts (id, owner_id, phone, session_str, device_model, system_version, app_version, trust_score, is_active)

-- Ботоуправление
managed_bots (bot_id, added_by, token, username, first_name, is_active)
tracked_keywords (id, bot_id, owner_id, keyword, is_active)
search_rankings (id, keyword_id, position, checked_at)

-- Операции
operation_queue (id, owner_id, op_type, status, params, total_items, done_items, scheduled_for)
operation_log (id, op_id, step_num, target, status, message)

-- Рестрикции и алерты
restriction_events (id, owner_id, account_id, bot_id, event_type, severity, details)
account_flood_log (id, account_id, operation, flood_seconds)
restriction_alert_cooldown (owner_id, event_type, entity_id, last_alerted)

-- Поведенческий слой
behavioral_events (id, owner_id, entity_type, entity_id, event_type, meta, occurred_at)
entity_behavioral_score (owner_id, entity_type, entity_id, attention_score, habit_score, ecosystem_score, decay_rate)
search_memory (owner_id, keyword, search_count, affinity_score)

-- Пользователи и монетизация
users (id, username, plan, plan_until)
notification_settings (user_id, new_user, flood_warning, position_change, op_complete, restriction)
referrals (referrer_id, referred_id, rewarded)
```

---

## 10. ПОДПИСКИ

| Тир | Цена | Лимит ботов | Ключевые фичи |
|---|---|---|---|
| free | $0 | 3 | Базовые операции, алерты, уведомления |
| starter | $9/мес | 10 | Расписание, отчёты, массовая публикация, поисковая память |
| pro | $25/мес | 30 | Создание каналов/групп, behavioral dashboard, кластеры |
| enterprise | $69/мес | ∞ | Всё, + свармы, эксперименты |

**Паттерн добавления gate:**
```python
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
if not await require_plan(pool, callback.from_user.id, "starter"):
    await _edit(callback, locked_text("Название", "starter"), subscription_locked_markup("starter"))
    return
```

---

## 11. BOTMOTHER OS — КАРТА МЕНЮ

```
/menu → BotMother OS
├── 🏗️ Infrastructure
│   ├── 📱 Аккаунты          → AccCb(action="menu")
│   ├── 🤖 Мои боты          → BotCb(action="list")
│   ├── 📡 Каналы & операции → ChanCb(action="menu")
│   ├── 👥 Группы            → GroupFCb(action="menu")
│   ├── 🔗 Кластеры          → ClustMCb(action="menu")
│   ├── 🌐 Прокси            → ProxyCb(action="menu")
│   └── ❤️ Здоровье          → HealthCb(action="menu")
├── 👁️ Visibility
│   ├── 🔍 Ключевые слова    → pick_bot_for → RankCb(action="menu")
│   ├── 📊 Позиции           → pick_bot_for → RankCb(action="menu")
│   ├── 🏆 Конкуренты        → CompCb(action="menu")
│   ├── 🔔 Алерты            → BmCb(action="alerts") [FREE]
│   └── 📋 Отчёты            → BmCb(action="vis_reports") [STARTER]
├── ⚙️ Operations
│   ├── ⚡ Массовые действия → BmCb(action="bulk_ops")
│   ├── 🛠️ Построитель       → MassOpCb(action="menu")
│   ├── 📋 Очередь           → MassOpCb(action="queue")
│   ├── ⏱️ Планировщик       → BmCb(action="op_planner")
│   ├── 📄 Шаблоны           → AssetTplCb(action="menu")
│   └── 📊 Отчёты            → BmCb(action="op_reports") [STARTER]
├── 📢 Broadcasts
│   ├── 📢 Рассылка по боту  → BotCb(action="list")
│   ├── 🌐 Сетевая рассылка  → NetBcCb(action="choose_target")
│   └── 📅 Расписание        → BmCb(action="schedules") → выбор бота → ScheduleCb
├── 💬 Inbox / Relay         → pick_bot_for → RelayCb(action="menu")
├── 🤖 AI Assistant          → AiCb(action="start")
├── 🧠 Аналитика             → BmCb(action="behavioral") [PRO]
│   ├── 📊 Топ по вниманию
│   ├── 🔄 Активные привычки
│   ├── 📉 Угасающие ресурсы
│   ├── 🌐 Экосистемные узлы
│   └── 🔍 Поисковая память
├── 💳 Billing               → SubCb(action="menu")
├── 👥 Referral              → RefCb(action="menu")
└── ⚙️ Settings
    ├── 📢 Авто-ответы       → pick_bot_for → AutoReplyCb(action="list")
    └── 🔔 Уведомления       → BmCb(action="notifications") [FREE]
```

---

## 12. ЗАВЕРШЁННЫЕ ФИЧИ (100% готово)

### Инфраструктура
- [x] Multi-account management (логин по QR/номеру/session, import/export)
- [x] Device fingerprints per account (20 уникальных Android-профилей)
- [x] Bot management (добавить, редактировать, команды, webhooks, multigeo)
- [x] Channel Factory (создать, bulk-создать, редактировать, статистика, ссылки, шаблоны)
- [x] Group Factory (создать, список, участники, объявление)
- [x] Cluster Manager (группировка каналов/ботов)
- [x] Proxy Manager (socks5, проверка, привязка)
- [x] Health Dashboard (состояние аккаунтов, trust scores)

### Операции
- [x] Mass Ops (bulk edit bots, bulk join/leave, операции с аккаунтами)
- [x] Operation Queue (очередь, прогресс, отмена)
- [x] Mass Publish (публикация во все каналы/кластеры/теги, задержки)
- [x] Network Broadcast (рассылка по сети ботов)
- [x] Asset Templates (шаблоны бот/канал/группа/пост + **Apply Template** 100%)
- [x] Channel Operations (join/leave/publish/edit/contacts — полный набор с anti-ban)

### Видимость
- [x] Search Rankings (трекинг позиций в поиске Telegram)
- [x] Search Observations (паттерн подтверждения изменений позиций)
- [x] Competitors (мониторинг конкурентов)
- [x] Visibility Reports (отчёт по позициям всех keywords) [STARTER]
- [x] Alerts (агрегация restriction_events, пагинация, очистка) [FREE]

### Поведенческий слой (NEW)
- [x] Behavioral Events (сырой лог событий)
- [x] Behavioral Engine (фоновый пересчёт attention/habit/ecosystem/decay)
- [x] Search Memory (keyword affinity, повторные поиски)
- [x] Behavioral Dashboard в BotMother [PRO]
- [x] Session Simulator (human_delay, bulk_item_pause, chaos_factor)

### Коммуникация
- [x] Relay (входящие диалоги, ответы от имени бота)
- [x] Auto-reply (правила авто-ответов)
- [x] CRM (пользователи ботов, теги, заметки, история)
- [x] Funnels (автоворонки с шагами и задержками)
- [x] Schedules (запланированные рассылки)
- [x] Broadcast (рассылка по боту с языковой сегментацией)

### Монетизация и настройки
- [x] Subscription (4 тира: free/starter/pro/enterprise, gates на 28+ фичах)
- [x] Payment Checker (фоновая проверка платежей)
- [x] Referral System (реферальная программа с tier-наградами)
- [x] AI Assistant (интеграция с Claude/Gemini API)
- [x] Notifications Settings (per-user toggle, таблица notification_settings) [FREE]

### Мониторинг и безопасность
- [x] Account Monitor (ограничения аккаунтов)
- [x] Trust Engine (scoring аккаунтов)
- [x] Shadowban Monitor (детекция теневых банов)
- [x] Operation Reports (история операций, статистика) [STARTER]

---

## 13. ИЗВЕСТНЫЕ ЛОВУШКИ

| Ловушка | Решение |
|---|---|
| `str = ""` в CallbackData → ValidationError | Всегда `Optional[str] = None` |
| Два handler на один prefix+action в разных роутерах | Первый зарегистрированный побеждает тихо |
| `asyncio.sleep(0.5)` в bulk → флуд-бан | Использовать `bulk_item_pause()` из session_simulator |
| Telethon без `_acc` → один device fingerprint на все аккаунты | Всегда передавать `_acc=acc` |
| `_progress_text` с разными сигнатурами в разных файлах | Только из `op_helpers`, кастомный title через параметр |
| `add_tracked_keyword` без behavioral collector | После вызова → `record_search_repeat` |
| `ScheduleCb(bot_id=0)` → "бот не найден" | Показывать bot-picker перед переходом |
| Прямой SQL с f-string для user input | ТОЛЬКО через параметры `$1, $2` asyncpg |
| `message.edit_text` на кнопку без `callback.answer()` | Всегда `await callback.answer()` первым |

---

## 14. РАБОЧИЙ ПРОЦЕСС

### Стандартная итерация
```bash
# 1. Убедиться в ветке
git checkout claude/telegram-bot-services-xfAh6

# 2. Сделать изменения, проверить синтаксис
cd tg-manager && python3 -c "import py_compile; py_compile.compile('path/to/file.py', doraise=True)"

# 3. Если новая БД-таблица → создать schema_v{N+1}.sql
# create_pool() подхватит автоматически

# 4. Коммит + пуш (stop-hook требует пуш)
git add tg-manager/path/to/file.py
git commit -m "feat/fix/refactor: краткое описание"
git push -u origin claude/telegram-bot-services-xfAh6
```

### Добавление нового handler-файла
```python
# 1. Создать bot/handlers/my_feature.py с router = Router()
# 2. Добавить в main.py:
from bot.handlers import my_feature as my_feature_handler
dp.include_router(my_feature_handler.router)  # в нужное место
# 3. Новый CallbackData → добавить в bot/callbacks.py
# 4. Новые FSM states → добавить в bot/states.py
```

### Добавление нового фонового сервиса
```python
# 1. Создать services/my_service.py с async def run(pool):
# 2. В main.py добавить:
from services import my_service
asyncio.create_task(my_service.run(pool))
```

---

## 15. СЛЕДУЮЩИЕ ПРИОРИТЕТЫ

### Высокий приоритет
- [ ] **Mass Publish + template prefill** — при создании из post-шаблона подставлять текст автоматически (сейчас redirect с подсказкой)
- [ ] **Operation Planner FSM** — полноценный wizard выбора операции + времени + `scheduled_for` в БД
- [ ] **Behavioral collectors** в channel_ops.py — `record_cross_nav` при переходах между разделами

### Средний приоритет
- [ ] **Notification delivery** — фактическая отправка уведомлений пользователям согласно `notification_settings`
- [ ] **Search Memory dashboard** — кнопки drill-down по keyword → показ истории позиций
- [ ] **Operation Builder FSM** — wizard создания сложных операций (сейчас redirect на mass_ops)

### Низкий приоритет
- [ ] **Export CSV** для visibility reports
- [ ] **Webhook для платежей** — заменить polling в payment_checker
- [ ] **Admin bulk tools** — массовые операции из admin panel

---

## 16. ДЕПЛОЙ

- **Платформа:** Railway
- **Root Directory:** `/tg-manager`
- **Ветка:** `claude/telegram-bot-services-xfAh6` → auto-deploy при пуше
- **Проверка после деплоя:** `/version` или `/menu` в боте
- **Логи:** Railway dashboard → Deployments → Latest
