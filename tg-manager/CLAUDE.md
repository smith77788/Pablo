# CLAUDE.md — BotMother OS: Главный ориентир системы (v3.2)

> Этот файл читается автоматически при каждой сессии Claude Code.
> Он — авторитетный постоянный контекст проекта. Обновлять при каждой значимой итерации.

---

## ОБЯЗАТЕЛЬНОЕ ЧТЕНИЕ ПЕРЕД КОДИНГОМ

Прочитать в указанном порядке (папка `.botmother/`):

1. `.botmother/00_READ_ME_FIRST.md` — главный запрет: не перестраивать
2. `.botmother/01_CORE_CONTEXT.md` — продуктовая суть BotMother
3. `.botmother/02_EXECUTION_PROTOCOL.md` — workflow перед кодингом
4. `.botmother/03_FEATURE_CATALOG.md` — полный каталог фич
5. `.botmother/19_ARCHITECTURE_GOVERNANCE.md` — архитектурные правила
6. `.botmother/20_OPERATION_ENGINE_CONTRACT.md` — контракт движка операций
7. `.botmother/21_DATABASE_GOVERNANCE.md` — правила работы с БД
8. `.botmother/22_FEATURE_PRIORITY_SCORING.md` — приоритизация задач
9. `.botmother/23_TELEGRAM_UX_GOVERNANCE.md` — UX-правила для Telegram
10. `.botmother/24_SELF_REVIEW_LOOP.md` — чеклист самопроверки перед коммитом
11. `.botmother/26_AUTONOMOUS_IMPLEMENTATION_LOOP.md` — протокол автономного режима
12. `.botmother/27_TASK_QUEUE_PROTOCOL.md` — протокол очереди задач

**Для автономного режима также читать:**
- `TASK_QUEUE.md` — активная очередь задач
- `CURRENT_STATE.md` — текущее состояние проекта
- `IMPLEMENTATION_LOG.md` — лог выполненных задач
- `AUTONOMOUS_CLAUDE_PROMPT.md` — системный промпт автономного режима
- `STOP_CONDITIONS.md` — условия остановки

**Затем** читать этот файл (CLAUDE.md) для project-specific контекста.

---

## 0. ПРАВИЛА СЕССИИ (всегда соблюдать)

```
1. Отвечать ТОЛЬКО на русском языке
2. Все изменения ТОЛЬКО в папке tg-manager/
3. НЕ удалять и НЕ ломать существующую функциональность
4. sessionEncrypted НИКОГДА не возвращать в API-ответах
5. Bot tokens — только в зашифрованном виде, никогда в логах
6. Никаких спам / обход / эвазия / абьюз workflows
7. Перед коммитом: python3 -c "import ast; ast.parse(open('file.py').read())"
8. Ветка разработки: claude/telegram-bot-services-xfAh6
9. git push после КАЖДОГО коммита (stop-hook требует)
10. Работать инкрементально — не перестраивать, расширять
```

---

## 1. ЧТО ТАКОЕ BOTMOTHER

**BotMother IS NOT:**
- простой конструктор ботов
- базовая admin-панель
- инструмент рассылок
- спам-инструмент
- игрушечный dashboard

**BotMother IS:**
# Telegram Infrastructure, Visibility & Account Operations OS

Корпоративная Telegram-native операционная система для управления Telegram-экосистемами в масштабе.

### Ощущение продукта
Пользователь должен чувствовать:
**"Я управляю Telegram-инфраструктурой, а не отдельными аккаунтами или ботами."**

Система должна ощущаться как:
- **Kubernetes** для Telegram-инфраструктуры
- **Datadog** для Telegram-операций
- **Bloomberg Terminal** для Telegram-видимости
- **Cloudflare** как оркестрационный слой для коммуникационной инфраструктуры

### Три уровня системы

```
ИНФРАСТРУКТУРА        ИНТЕЛЛЕКТ              МОНЕТИЗАЦИЯ
─────────────────     ──────────────────     ──────────────────
Аккаунты              Behavioral Engine      Подписки (4 тира)
Боты                  Search Rankings        Реферальная сеть
Каналы                Trust Scores           Payment Checker
Группы                Shadowban Monitor      Operation Reports
Кластеры              AI Assistant           CRM + Funnels
Прокси                Cohort Analytics       Auto-reply
Сессии                Predictive Timing      Experiments (A/B)
```

---

## 2. ФИЛОСОФИЯ ПРОДУКТА

### 2.1 Telegram-Native — КРИТИЧНО

BotMother является **TELEGRAM-NATIVE** платформой.

**ОСНОВНОЙ интерфейс** — НЕ веб-дашборд, а **бот BotMother в Telegram**.

Всё важное должно управляться напрямую внутри Telegram через:
- bot-меню и inline keyboards
- callback-кнопки и wizard-потоки
- пошаговые FSM-диалоги
- пагинированные списки
- Telegram-уведомления
- Telegram-delivered отчёты/файлы

**Web UI / Mini App** — опционально и вторично:
- большие таблицы / графики
- топологические карты
- расширенные фильтры
- детальная аналитика

### 2.2 Предиктивное выполнение (не реактивное)

BotMother НЕ должен быть реактивным. Система должна проактивно понимать безопасные тайминги **ДО** выполнения.

```
Принцип: Predictive Timing & Limit-Aware Execution

✓ Рассчитывать безопасный темп до запуска
✓ Распределять нагрузку между аккаунтами
✓ Избегать bursts операций
✓ Прогнозировать нагрузку на аккаунт/кластер
✓ Балансировать выполнение
✓ Предотвращать перегрузку операциями
```

**При hard limit от Telegram:**
- Пауза затронутых операций → пересчёт тайминга → ребаланс → снижение плотности
- Лог события → нейтральное сообщение: "Тайминг выполнения автоматически скорректирован"
- НИКОГДА не обходить ограничения, не форсировать ретраи, не эвейдить

### 2.3 Инфраструктурный подход

Каждый актив (аккаунт, бот, канал, группа) — это **ресурс инфраструктуры** с:
- `health_score`
- `trust_score`
- `visibility_score`
- `activity_score`
- `tags` / `cluster`
- историей операций
- аудитом
- нотами

### 2.4 Governance (управляемость)

Каждая операция должна поддерживать:
- проверки прав
- approval workflows (для критических действий)
- audit logs
- dry-run / preview
- прогноз тайминга / нагрузки
- rollback где возможно

---

## 3. ТЕХНИЧЕСКАЯ АРХИТЕКТУРА

### 3.1 Стек
| Компонент | Технология |
|-----------|-----------|
| Bot Framework | aiogram 3.13.1 + Pydantic v2 |
| Database | PostgreSQL через asyncpg (Railway) |
| Telegram API | Telethon (userbot) + Bot API |
| Deploy | Railway, Root Dir = `/tg-manager`, auto-deploy |
| Branch | `claude/telegram-bot-services-xfAh6` |

### 3.2 Структура файлов

```
tg-manager/
├── main.py                        # точка входа, регистрация роутеров + сервисов
├── config.py                      # BOT_TOKEN, DB_URL, ADMIN_IDS, ENCRYPTION_KEY
├── database/
│   └── db.py                      # 163+ функций, create_pool() авто-мигрирует schema_v*.sql
├── bot/
│   ├── callbacks.py               # ВСЕ CallbackData классы — только здесь
│   ├── states.py                  # ВСЕ FSMState классы — только здесь
│   ├── keyboards.py               # shared keyboards (main_menu, subscription_locked_markup)
│   ├── handlers/                  # 44+ файла обработчиков
│   │   ├── botmother_menu.py      # главное меню OS — точка входа /menu
│   │   ├── start.py               # /start, /help, /version, /cancel
│   │   ├── accounts.py            # Telegram-аккаунты (QR/phone/session)
│   │   ├── bots.py                # Bot management
│   │   ├── channel_factory.py     # Channel Factory (создать, импорт, bulk, редактировать)
│   │   ├── channel_ops.py         # Account Operations (join/leave/post/edit/bulk)
│   │   ├── group_factory.py       # Group Factory (создать, импорт, участники)
│   │   ├── mass_publish.py        # Mass Publish wizard
│   │   ├── mass_ops.py            # Operation Builder / Queue
│   │   ├── ranking.py             # Search Rankings + Keywords
│   │   ├── asset_templates.py     # Шаблоны активов (бот/канал/группа/пост)
│   │   ├── health_dashboard.py    # Health Dashboard аккаунтов
│   │   ├── behavioral_engine.py   # в services/ — поведенческий анализ
│   │   └── [40+ других handlers]
│   └── utils/
│       ├── op_helpers.py          # _acc_label, _get_active_accounts, _progress_bar/text/format
│       └── subscription.py        # require_plan(), get_plan(), locked_text(), is_platform_admin()
└── services/
    ├── account_manager.py         # ВСЕ Telethon-операции (singleton паттерн)
    ├── behavioral_engine.py       # поведенческие оценки (каждые 15 мин)
    ├── session_simulator.py       # human-like delays (beta-распределение)
    ├── account_monitor.py         # мониторинг ограничений аккаунтов
    ├── trust_engine.py            # обновление trust_score аккаунтов
    ├── shadowban_monitor.py       # детекция теневых банов
    ├── op_worker.py               # воркер очереди операций
    ├── ranking_checker.py         # периодическая проверка позиций
    ├── scheduler.py               # запуск расписаний рассылок
    ├── auto_responder.py          # авто-ответы на сообщения
    ├── relay.py                   # live relay диалогов
    ├── funnel_runner.py           # воронки (цепочки шагов)
    ├── payment_checker.py         # проверка платежей
    ├── search_observer.py         # наблюдение за поисковыми паттернами
    ├── broadcaster.py             # рассылки
    ├── routing_engine.py          # маршрутизация между ботами
    ├── bot_api.py                 # Bot API wrapper
    └── railway_api.py             # Railway API интеграция
```

### 3.3 Авто-миграция БД

`create_pool()` автоматически выполняет все `schema_v*.sql` в порядке версии.
Текущая последняя версия: **v43**

Правило: новая схема → новый файл `schema_v{N+1}.sql` в корне `tg-manager/`.

---

## 4. КРИТИЧЕСКИЕ ПАТТЕРНЫ КОДА

### 4.1 CallbackData — Pydantic v2 (ОБЯЗАТЕЛЬНО)

```python
# ✅ ПРАВИЛЬНО — Optional для полей которые могут быть пустыми
class BmCb(CallbackData, prefix="bm"):
    action: str
    sub: Optional[str] = None   # НЕ str = ""
    page: int = 0

# ❌ НЕПРАВИЛЬНО — ValidationError при десериализации пустых сегментов
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

### 4.3 Telethon — всегда передавать _acc

```python
# ✅ ПРАВИЛЬНО — device fingerprint из аккаунта
result = await account_manager.some_operation(
    acc["session_str"],
    arg1, arg2,
    _acc=acc,               # dict с полями acc (device_model, system_version, app_version)
)

# ❌ НЕПРАВИЛЬНО — один fingerprint на все аккаунты (риск бана)
result = await account_manager.some_operation(acc["session_str"], arg1)
```

### 4.4 Общие хелперы — только из op_helpers

```python
# ✅ ПРАВИЛЬНО
from bot.utils.op_helpers import (
    _acc_label, _get_active_accounts,
    _progress_bar, _progress_text, _format_duration,
)

# ❌ НЕПРАВИЛЬНО — НЕ определять эти функции локально в handler-файлах
def _acc_label(acc): ...       # ДУБЛИКАТ — удалить
```

### 4.5 FSM Template Prefill паттерн

```python
# В asset_templates.py при применении шаблона:
await state.update_data(tpl_prefill={
    "title": "...", "description": "...", "username": "...",
})
await state.set_state(ChannelFactoryFSM.choosing_account)

# В account_chosen handler — проверить prefill и пропустить FSM-шаги:
sd = await state.get_data()
prefill = sd.get("tpl_prefill") or {}
if prefill.get("title"):
    await state.update_data(
        title=prefill["title"],
        about=prefill.get("description", ""),
        tpl_prefill=None,
    )
    await _show_chanf_cluster_or_confirm(callback, state, pool)
    return
```

### 4.6 Anti-ban задержки — умный подход

```python
# channel_factory.py bulk create — ТЕКУЩИЙ ПАТТЕРН
from services import session_simulator

for i, item in enumerate(items):
    await session_simulator.typing_delay(item_name)  # перед действием
    # ... выполнить операцию ...
    if i < len(items) - 1:
        if i % 5 == 0:  # каждые 5 — длинная пауза
            delay = random.uniform(300, 600) * session_simulator.chaos_factor()
        else:
            delay = random.uniform(45, 90) * session_simulator.chaos_factor()
        await asyncio.sleep(delay)

# channel_ops.py bulk create — ТЕКУЩИЙ ПАТТЕРН
chaos = session_simulator.chaos_factor()
await asyncio.sleep(max(backoff, flood, base_delay * chaos))
# Каждые 5 операций: base_delay = 120.0 (cooldown)
```

### 4.7 Import существующих активов (NEW паттерн)

```python
# Получить каналы из аккаунта и сохранить в managed_channels
from services import account_manager
from database.db import upsert_managed_channels

dialogs = await account_manager.get_dialogs(acc["session_str"], limit=200, _acc=acc) or []
channels = [d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")]
await upsert_managed_channels(pool, owner_id, acc["id"], channels)
```

### 4.8 Безопасный SQL — всегда параметризованные запросы

```python
# ✅ ПРАВИЛЬНО
await pool.execute("UPDATE tg WHERE id=$1 AND owner_id=$2", item_id, user_id)

# ❌ НЕПРАВИЛЬНО — SQL-инъекция
await pool.execute(f"UPDATE tg WHERE id={item_id}")
```

---

## 5. ВСЕ CALLBACK-ПРЕФИКСЫ (52 штуки, все уникальны)

```
bot  edit  aud  wh   bc   bulk  cmd  tpl  sch  mg
ar   rl    fn   st   note sw    crm  au   exp  dl
eng  seo   net  cl   sub  ai    nbc  acc  rank chan
cinv ref   bm   atpl grpf mop   btf  chanf mpub comp
vis  hlth  prx  clm  gp   tba   lib  prs  wu   infra
cln  dm
```

**Правило:** новый CallbackData → новый уникальный prefix в `bot/callbacks.py`.

---

## 6. ЗАРЕГИСТРИРОВАННЫЕ РОУТЕРЫ В main.py (44+ штук)

Порядок регистрации важен — более специфичные роутеры раньше:
1. `bm_handler` (botmother_menu) — первый
2. Factory handlers: bot_factory, group_factory, chan_factory
3. Operation handlers: mass_ops, asset_tpl, mass_pub, competitors
4. Sub/billing: sub_handler
5. Core: start, bots, edit, audience, webhooks, broadcast, etc.
6. `relay_handler` — перед последними (ловит F.reply_to_message)
7. `admin_handler` — самый последний

---

## 7. ФОНОВЫЕ СЕРВИСЫ (16 штук в main.py)

```python
asyncio.create_task(scheduler.run(pool, http))
asyncio.create_task(auto_responder.run(pool, http, bot))
asyncio.create_task(relay_service.run(pool, http))
asyncio.create_task(funnel_runner.run(pool, http))
asyncio.create_task(payment_checker.run(pool, http, bot))
asyncio.create_task(ranking_checker.run(pool, bot))
asyncio.create_task(search_observer.run_confirmation_loop(pool, bot))
asyncio.create_task(account_monitor.run(pool, bot))
asyncio.create_task(trust_engine.run(pool))
asyncio.create_task(shadowban_monitor.run(pool, bot))
asyncio.create_task(op_worker.run(pool, bot))
asyncio.create_task(behavioral_engine.run(pool))
asyncio.create_task(account_warmer.run_warmup_loop(pool))
asyncio.create_task(account_health.run_health_check_loop(pool))
asyncio.create_task(payment_webhook.run(pool, bot))  # HTTP :8080
```

---

## 8. АНТИ-БАН СИСТЕМА

### Device Fingerprints
- 20 уникальных Android-профилей в `account_manager.py:_ANDROID_DEVICES`
- Каждый аккаунт имеет `device_model`, `system_version`, `app_version` в БД (schema_v23)
- `generate_device_fingerprint()` → рандомный профиль при создании
- `_make_client(session_str, _acc)` → использует сохранённый профиль аккаунта

### Flood Protection
- Exponential backoff: `_backoff(attempt, base=2.0, cap=60.0)`
- Flood wait respect: `asyncio.sleep(max(backoff, flood_seconds))`
- `session_simulator.chaos_factor()` — мультипликатор 0.7–1.3 для рандомизации
- `session_simulator.typing_delay(text)` — пауза перед каждым действием
- Batch паузы: каждые 5 операций → длинный cooldown 300-600s

### Account Trust System
- `trust_engine` → обновляет `trust_score` каждого аккаунта
- `account_monitor` → следит за ограничениями
- `shadowban_monitor` → детектирует теневые баны
- Операции используют аккаунты с наивысшим `trust_score` (ORDER BY trust_score DESC NULLS LAST)
- При бане: `deactivate_account()` + `record_flood_event()`

### Session Simulator (services/session_simulator.py)
```python
human_delay(1.5, 8.0)       # beta-распределение — не равномерное
short_pause(0.3, 1.5)        # быстрая пауза
typing_delay(text)            # симуляция набора текста
bulk_item_pause(i, 10)       # длинная пауза каждые 10 элементов
chaos_factor(base, spread)   # мультипликатор для рандомизации
```

### Behavioral Engine (services/behavioral_engine.py)
Собирает поведенческие события → вычисляет каждые 15 мин:
- `attention_score` — устойчивость внимания (0–100)
- `habit_score` — привычность, регулярность по неделям (0–100)
- `ecosystem_score` — встроенность (cross-nav links) (0–100)
- `decay_rate` — скорость угасания per day

---

## 9. СХЕМА БД (57+ таблиц, v33 схем)

```sql
-- Инфраструктура
tg_accounts      (id, owner_id, phone, session_str, device_model, system_version, app_version,
                  trust_score, flood_count_7d, cooldown_until, last_flood_at, is_active, cluster)
managed_bots     (bot_id, added_by, token_encrypted, username, first_name, is_active)
managed_channels (owner_id, acc_id, channel_id, title, username, access_hash)
clusters         (id, owner_id, name, created_at)

-- Видимость
tracked_keywords  (id, bot_id, owner_id, keyword, is_active)
search_rankings   (id, keyword_id, position, checked_at)
search_snapshots  (id, keyword_id, raw_json, checked_at)
search_memory     (owner_id, keyword, search_count, affinity_score)

-- Операции
operation_queue  (id, owner_id, op_type, status, params, total_items, done_items,
                  scheduled_for, template_id, created_at)
operation_log    (id, op_id, step_num, target, status, message)

-- Уведомления и алерты
restriction_events (id, owner_id, account_id, bot_id, event_type, severity, details, created_at)
account_flood_log  (id, account_id, operation, flood_seconds, created_at)
notification_settings (user_id, new_user, flood_warning, position_change, op_complete, restriction)

-- Поведенческий слой
behavioral_events        (id, owner_id, entity_type, entity_id, event_type, meta, occurred_at)
entity_behavioral_score  (owner_id, entity_type, entity_id, attention_score, habit_score,
                          ecosystem_score, decay_rate, reentry_count, updated_at)

-- Пользователи и монетизация
platform_users   (user_id, username, first_name, last_active, created_at)
users            (id, username, plan, plan_until)
platform_referral_codes (user_id, code, created_at)
platform_referrals      (referrer_id, referred_id, activated_at, paid_at)
referral_rewards        (user_id, level, plan, days, given_at)

-- Боты-специфика (per bot)
bot_users, broadcasts, scheduled_broadcasts, templates, auto_replies,
funnel_sequences, funnel_subscribers, relay_sessions, relay_messages,
crm_contacts, crm_tags, crm_notes, ab_experiments, deep_links, etc.
```

---

## 10. ПОДПИСКИ

| Тир | Цена | Лимит ботов | Лимит аккаунтов | Ключевые фичи |
|---|---|---|---|---|
| free | $0 | 3 | 2 | Базовые операции, алерты, уведомления |
| starter | $9/мес | 10 | 5 | Расписание, отчёты, публикация, поисковая память |
| pro | $25/мес | 30 | 15 | Создание каналов/групп, behavioral dashboard, кластеры |
| enterprise | $69/мес | ∞ | ∞ | Всё, + свармы, эксперименты, мультигео |

**Subscription gate паттерн:**
```python
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup

if not await require_plan(pool, callback.from_user.id, "starter"):
    await _edit(callback, locked_text("Название", "starter"), subscription_locked_markup("starter"))
    return
```

---

## 11. BOTMOTHER OS — КАРТА МЕНЮ (актуальная)

```
/menu → BotMother OS
├── 🏗️ Infrastructure
│   ├── 📱 Аккаунты           → AccCb(action="menu")
│   ├── 🤖 Мои боты           → BotCb(action="list")
│   ├── 📡 Каналы & операции  → ChanCb(action="menu")
│   │   └── ChanFactCb(action="menu")
│   │       ├── ➕ Создать канал
│   │       ├── 📋 Массовое создание
│   │       ├── 📥 Импорт из Telegram  ← NEW
│   │       ├── ✏️ Редактировать
│   │       ├── 📤 Массовая публикация
│   │       ├── 📊 Статистика
│   │       └── 🔗 Генерация ссылок
│   ├── 👥 Группы             → GroupFCb(action="menu")
│   │   ├── ➕ Создать группу
│   │   ├── 📥 Импорт из Telegram  ← NEW
│   │   ├── 📋 Мои группы
│   │   ├── 👥 Участники
│   │   └── 📢 Объявление
│   ├── 🔗 Кластеры           → ClustMCb(action="menu")
│   ├── 🌐 Прокси             → ProxyCb(action="menu")
│   └── ❤️ Здоровье           → HealthCb(action="menu")
├── 👁️ Visibility
│   ├── 🔍 Ключевые слова     → pick_bot_for → RankCb(action="menu")
│   ├── 📊 Позиции            → pick_bot_for → RankCb(action="menu")
│   ├── 🏆 Конкуренты         → CompCb(action="menu")
│   ├── 🔔 Алерты             → BmCb(action="alerts") [FREE]
│   └── 📋 Отчёты             → BmCb(action="vis_reports") [STARTER]
├── ⚙️ Operations
│   ├── ⚡ Массовые действия  → BmCb(action="bulk_ops")
│   │   ├── 🤖 Боты           → NetworkCb(action="menu")
│   │   ├── 📡 Каналы bulk    → ChanCb(action="bulk_menu")
│   │   ├── 📤 Публикация     → MassPubCb(action="menu")
│   │   └── 📱 Аккаунты bulk  → ChanCb(action="bulk_menu")
│   ├── 🛠️ Построитель        → MassOpCb(action="menu")
│   ├── 📋 Очередь            → MassOpCb(action="queue")
│   ├── ⏱️ Планировщик        → BmCb(action="op_planner")
│   ├── 📄 Шаблоны            → AssetTplCb(action="menu")
│   └── 📊 Отчёты             → BmCb(action="op_reports") [STARTER]
├── 📢 Broadcasts
│   ├── 📢 Рассылка по боту   → BotCb(action="list")
│   ├── 🌐 Сетевая рассылка   → NetBcCb(action="choose_target")
│   └── 📅 Расписание         → BmCb(action="schedules")
├── 💬 Inbox / Relay          → pick_bot_for → RelayCb(action="menu")
├── 🤖 AI Assistant           → AiCb(action="start")
├── 🧠 Аналитика              → BmCb(action="behavioral") [PRO]
│   ├── 📊 Топ по вниманию
│   ├── 🔄 Активные привычки
│   ├── 📉 Угасающие ресурсы
│   ├── 🌐 Экосистемные узлы
│   └── 🔍 Поисковая память
├── 💳 Billing                → SubCb(action="menu")
├── 👥 Referral               → RefCb(action="menu")
└── ⚙️ Settings
    ├── 📢 Авто-ответы         → pick_bot_for → AutoReplyCb(action="list")
    └── 🔔 Уведомления         → BmCb(action="notifications") [FREE]
```

---

## 12. ИНВЕНТАРЬ ФИЧ (актуальный статус)

### ✅ Инфраструктура — ГОТОВО
- Multi-account management (QR/номер/session/import)
- Device fingerprints per account (20 Android-профилей, schema_v23)
- **Импорт существующих каналов** из Telegram в managed_channels (NEW)
- **Импорт существующих групп** из Telegram в managed_channels (NEW)
- Bot management (добавить, токен, команды, webhooks, multigeo)
- Channel Factory (создать, bulk-создать, импорт, редактировать, статистика, ссылки)
- Group Factory (создать, импорт, список, участники, объявление)
- Cluster Manager (группировка каналов/ботов)
- Proxy Manager (socks5, проверка, привязка)
- Health Dashboard (состояние аккаунтов, trust scores) — исправлен cooldown_until

### ✅ Операции — ГОТОВО
- Mass Ops (bulk edit bots, bulk join/leave)
- Operation Queue (очередь, прогресс, отмена)
- Mass Publish (все каналы / по аккаунту / dry-run + **Умный тайминг** 30-90s)
- Network Broadcast (рассылка по сети ботов)
- Asset Templates + Apply Template (100%)
- Channel Operations (join/leave/publish/edit/contacts — полный набор)
- **Умные тайминги в bulk create**: typing_delay + 45-90s chaos + 5-10 мин каждые 5

### ✅ Видимость — ГОТОВО
- Search Rankings (трекинг позиций)
- Search Observations (паттерн подтверждения)
- Competitors (мониторинг конкурентов)
- Visibility Reports [STARTER]
- Alerts (агрегация restriction_events) [FREE/STARTER]

### ✅ Поведенческий слой — ГОТОВО
- Behavioral Events log
- Behavioral Engine (attention/habit/ecosystem/decay каждые 15 мин)
- Search Memory (keyword affinity)
- Behavioral Dashboard [PRO]
- Session Simulator (интегрирован в channel_factory + channel_ops)

### ✅ Коммуникация — ГОТОВО
- Relay (входящие диалоги, ответы от имени бота)
- Auto-reply (правила, триггеры)
- CRM (теги, заметки, история пользователей)
- Funnels (автоворонки со шагами и задержками)
- Schedules (запланированные рассылки)
- Broadcast (рассылка с языковой сегментацией)

### ✅ Монетизация и настройки — ГОТОВО
- Subscription (4 тира + gates на 28+ фичах)
- Payment Checker (фоновая проверка)
- Referral System (tier-награды, лидерборд)
- AI Assistant (Claude/Gemini API)
- Notifications Settings (per-user toggle) [FREE]

### ✅ Мониторинг и безопасность — ГОТОВО
- Account Monitor (ограничения)
- Trust Engine (scoring)
- Shadowban Monitor
- Operation Reports [STARTER]

### ✅ UX-улучшения (NEW) — ГОТОВО
- Описания всех разделов в BotMother OS меню
- Онбординг с тремя сценариями для новых пользователей
- Статус-иконки ✅/⛔ в списке аккаунтов
- Описания в Channel Factory, Group Factory, Mass Publish
- Описания в Visibility, Operations, Broadcasts, Inbox, Settings

---

## 13. GAP-АНАЛИЗ (что ещё не реализовано)

### ✅ ВСЕ КРИТИЧЕСКИЕ ПРОБЕЛЫ ЗАКРЫТЫ (r11)

| Функция | Статус | Файл |
|---------|--------|------|
| Operation Planner FSM | ✅ | botmother_menu.py |
| record_reentry в start.py | ✅ | start.py |
| record_cross_nav при навигации | ✅ | botmother_menu.py |
| Notification delivery | ✅ | db.notify_if_enabled → account_monitor/ranking |
| Post template prefill | ✅ | mass_publish.py |
| DM-кампании | ✅ | dm_campaigns.py + dm_engine.py |
| Retry Intelligence | ✅ | op_worker.py _classify_op_error + _maybe_requeue |
| Invite Distribution Engine | ✅ | services/invite_engine.py |
| Geo Router | ✅ | services/geo_router.py |
| Capacity Planner | ✅ | services/capacity_planner.py |
| Admin bulk tools | ✅ | admin.py (bulk grant + cleanup + platform ops) |
| Payment Webhook | ✅ | services/payment_webhook.py (port 8080) |
| Funnel referral conversions | ✅ | funnel_runner.py |
| Session Converter | ✅ | services/session_converter.py |
| Account Cleaner | ✅ | services/account_cleaner.py + handler |

### 🟢 Низкий приоритет (nice to have)

| Пробел | Описание |
|--------|---------|
| Telegram Mini App | Веб-дашборд для таблиц/графиков (опционально) |
| Unified Asset Registry UI | Единый список всех активов с фильтрами |
| RBAC / Multi-user workspaces | Несколько пользователей в одной организации |
| Approval workflows | Подтверждение перед критическими bulk-операциями |
| Topology map | Граф связей ботов/каналов/аккаунтов |

---

## 14. ИЗВЕСТНЫЕ ЛОВУШКИ

| Ловушка | Решение |
|---------|---------|
| `str = ""` в CallbackData → ValidationError | Всегда `Optional[str] = None` |
| Два handler на один prefix+action | Первый зарегистрированный побеждает тихо |
| `asyncio.sleep(0.5)` в bulk → флуд-бан | Использовать `session_simulator` |
| Telethon без `_acc` → один fingerprint | Всегда передавать `_acc=acc` |
| `_progress_text` дублируется в файлах | Только из `op_helpers`, кастомный title через параметр |
| `add_tracked_keyword` без behavioral | После → `record_search_repeat` |
| `ScheduleCb(bot_id=0)` → бот не найден | Показывать bot-picker перед переходом |
| f-string SQL → инъекция | ТОЛЬКО параметры `$1, $2` asyncpg |
| `message.edit_text` без `callback.answer()` | Всегда `await callback.answer()` первым |
| `flood_wait_until` → не существует | Правильная колонка: `cooldown_until` (schema_v24) |
| `account_flood_log.owner_id` → нет поля | JOIN с `tg_accounts` для получения owner_id |

---

## 15. РАБОЧИЙ ПРОЦЕСС

### Стандартная итерация

```bash
# 1. Убедиться в ветке
git branch  # должна быть claude/telegram-bot-services-xfAh6

# 2. Сделать изменения, проверить синтаксис ВСЕХ изменённых файлов
python3 -c "
import ast, sys
files = ['path/to/file.py']
for f in files:
    try:
        ast.parse(open(f).read())
    except SyntaxError as e:
        print(f'ERROR {f}: {e}'); sys.exit(1)
print('All OK')
"

# 3. Если новая БД-таблица → создать schema_v{N+1}.sql
# create_pool() подхватит автоматически при деплое

# 4. Коммит + пуш (stop-hook требует пуш)
git add tg-manager/path/to/file.py
git commit -m "feat/fix/refactor: краткое описание"
git push -u origin claude/telegram-bot-services-xfAh6
```

### Добавление нового handler-файла

```python
# 1. Создать bot/handlers/my_feature.py с router = Router()
# 2. Добавить в main.py (в нужное место по приоритету):
from bot.handlers import my_feature as my_feature_handler
dp.include_router(my_feature_handler.router)
# 3. Новый CallbackData → добавить в bot/callbacks.py (уникальный prefix)
# 4. Новые FSM states → добавить в bot/states.py
```

### Добавление нового фонового сервиса

```python
# 1. Создать services/my_service.py с async def run(pool):
# 2. В main.py:
from services import my_service
asyncio.create_task(my_service.run(pool))
```

---

## 16. СЛЕДУЮЩИЕ ПРИОРИТЕТЫ

### 🔴 Высокий приоритет

1. **Operation Planner FSM** (BotMother → Operations → Планировщик)
   - FSM: выбор операции → выбор времени → confirm → `operation_queue.scheduled_for`
   - `op_worker.run()` уже проверяет `scheduled_for` — нужен только UI

2. **Notification Delivery**
   - При каждом restriction_event / flood / position_change → проверить `notification_settings`
   - Отправить `bot.send_message(user_id, ...)` если соответствующий флаг включён
   - Добавить вызов в `account_monitor.py` и `ranking_checker.py`

3. **Post Template → Mass Publish auto-inject**
   - В `asset_templates.py` при apply post-шаблона: `state.update_data(tpl_prefill={...})`
   - В `mass_publish.py` `cb_mpub_start`: проверить `tpl_prefill` и подставить текст автоматически

4. **Behavioral collectors включить**
   - `record_reentry` в `start.py` при возврате после 7+ дней отсутствия
   - `record_cross_nav` в ключевых навигационных переходах botmother_menu.py

### 🟡 Средний приоритет

5. **Operation Builder FSM** — полноценный wizard сборки операции из блоков
6. **Experiment conversion** — вызывать `record_experiment_conversion` из auto_responder.py
7. **Visibility Report → CSV export** — генерировать файл и отправлять в чат
8. **Search Memory drill-down** — из behavioral dashboard → история по конкретному keyword

### 🟢 Низкий приоритет

9. Export CSV для любых списков
10. Webhook для платежей (заменить polling)
11. Admin bulk tools
12. Telegram Mini App для аналитики

---

## 17. ДЕПЛОЙ

- **Платформа:** Railway
- **Root Directory:** `/tg-manager`
- **Ветка:** `claude/telegram-bot-services-xfAh6` → auto-deploy при пуше
- **Build:** `pip install -r requirements.txt && python main.py`
- **Проверка после деплоя:** `/version` или `/menu` в боте
- **Текущая build:** `2026.05.29-r11`
- **Логи:** Railway dashboard → Deployments → Latest

---

## 18. ПРИНЦИПЫ UX (для Telegram-native интерфейса)

1. **Каждое меню** должно начинаться с краткого описания что это такое и зачем
2. **Каждая кнопка** должна быть понятна без объяснений — emoji + понятное название
3. **Сложные операции** → пошаговый FSM wizard с возможностью отмены на каждом шаге
4. **Bulk-операции** → всегда показывать прогресс + итог
5. **Ошибки** → конкретное описание что не так и как исправить
6. **Новые пользователи** → онбординг с тремя сценариями использования
7. **Subscription gates** → объяснять ЧТО закрыто и предлагать апгрейд
8. **Деструктивные действия** → подтверждение с preview перед выполнением
9. **Пустые состояния** → объяснять что добавить и как это сделать
10. **Возврат назад** → кнопка "◀️ Назад" всегда должна быть на каждом экране

---

_Последнее обновление: 2026-05-29 (r11)_
_Следующий build-номер: r12_
