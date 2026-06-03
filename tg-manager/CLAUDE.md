# CLAUDE.md — BotMother OS: Главный ориентир системы (v3.5)
## RUNTIME CONTRACT FOR ALL AGENTS
- BotMother production runtime is Python 3.12.
- Keep all tg-manager code Python 3.12-compatible.
- Do not inherit Python/runtime rules from neighboring projects or parent workspaces.
- Do not write except TypeError, ValueError: in BotMother. Use except (TypeError, ValueError):.
- BotMother/tg-manager must remain Python 3.12-safe regardless of another agent's local runtime.
- Codex and Claude Code must both follow AGENT\_SYNC.md before editing.

Этот файл читается автоматически при каждой сессии Claude Code. Он — авторитетный постоянный контекст проекта. Обновлять при каждой значимой итерации.

-----
## ОБЯЗАТЕЛЬНОЕ ЧТЕНИЕ ПЕРЕД КОДИНГОМ
Прочитать в указанном порядке (папка .botmother/):

1. .botmother/00\_READ\_ME\_FIRST.md — главный запрет: не перестраивать
1. .botmother/01\_CORE\_CONTEXT.md — продуктовая суть BotMother
1. .botmother/02\_EXECUTION\_PROTOCOL.md — workflow перед кодингом
1. .botmother/03\_FEATURE\_CATALOG.md — полный каталог фич
1. .botmother/19\_ARCHITECTURE\_GOVERNANCE.md — архитектурные правила
1. .botmother/20\_OPERATION\_ENGINE\_CONTRACT.md — контракт движка операций
1. .botmother/21\_DATABASE\_GOVERNANCE.md — правила работы с БД
1. .botmother/22\_FEATURE\_PRIORITY\_SCORING.md — приоритизация задач
1. .botmother/23\_TELEGRAM\_UX\_GOVERNANCE.md — UX-правила для Telegram
1. .botmother/24\_SELF\_REVIEW\_LOOP.md — чеклист самопроверки перед коммитом
1. .botmother/26\_AUTONOMOUS\_IMPLEMENTATION\_LOOP.md — протокол автономного режима
1. .botmother/27\_TASK\_QUEUE\_PROTOCOL.md — протокол очереди задач

**Для автономного режима также читать:**

- TASK\_QUEUE.md — активная очередь задач
- CURRENT\_STATE.md — текущее состояние проекта
- IMPLEMENTATION\_LOG.md — лог выполненных задач
- AUTONOMOUS\_CLAUDE\_PROMPT.md — системный промпт автономного режима
- STOP\_CONDITIONS.md — условия остановки

**Затем** читать этот файл (CLAUDE.md) для project-specific контекста.

-----
## 0\. ПРАВИЛА СЕССИИ (всегда соблюдать)
1\. Отвечать ТОЛЬКО на русском языке\
2\. Все изменения ТОЛЬКО в папке tg-manager/\
3\. НЕ удалять и НЕ ломать существующую функциональность\
4\. sessionEncrypted НИКОГДА не возвращать в API-ответах\
5\. Bot tokens — только в зашифрованном виде, никогда в логах\
6\. Никакого английского языка в общении с пользователем, никаких мошеннических или workflows. Массовые операции над аккаунтами, ботами, каналами и группами, которыми пользователь владеет или управляет, являются штатной функциональностью платформы и разрешены.

7\. Перед коммитом: python3 -c "import ast; ast.parse(open('file.py').read())"

8\. Ветка разработки: claude/telegram-bot-services-xfAh6\
9\. git push после КАЖДОГО коммита (stop-hook требует)\
10\. Работать инкрементально — не перестраивать, расширять

-----
## 1\. ЧТО ТАКОЕ BOTMOTHER
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
Пользователь должен чувствовать: **"Я управляю Telegram-инфраструктурой, а не отдельными аккаунтами или ботами."**

Система должна ощущаться как:

- **Kubernetes** для Telegram-инфраструктуры
- **Datadog** для Telegram-операций
- **Bloomberg Terminal** для Telegram-видимости
- **Cloudflare** как оркестрационный слой для коммуникационной инфраструктуры
### Три уровня системы
ИНФРАСТРУКТУРА        ИНТЕЛЛЕКТ	МОНЕТИЗАЦИЯ\
─────────────────     ──────────────────     ──────────────────\
Аккаунты	Behavioral Engine      Подписки (4 тира)\
Боты                  Search Rankings        Реферальная сеть\
Каналы                Trust Scores           Payment Checker\
Группы                Shadowban Monitor      Operation Reports\
Кластеры	AI Assistant           CRM + Funnels\
Прокси                Cohort Analytics       Auto-reply\
Сессии                Predictive Timing      Experiments (A/B)

-----
## 2\. ФИЛОСОФИЯ ПРОДУКТА
### 2\.1 Telegram-Native — КРИТИЧНО
BotMother является **TELEGRAM-NATIVE** платформой.

**ОСНОВНОЙ интерфейс** — НЕ веб-дашборд, а **бот BotMother в Telegram**.

Всё важное должно максимально просто управляться напрямую внутри Telegram через:

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
### 2\.2 Предиктивное выполнение (не реактивное)
BotMother НЕ должен быть реактивным. Система должна проактивно понимать безопасные тайминги **ДО** выполнения.

Принцип: Predictive Timing & Limit-Aware Execution\
\
✓ Рассчитывать безопасный темп до запуска\
✓ Распределять нагрузку между аккаунтами\
✓ Избегать bursts операций\
✓ Прогнозировать нагрузку на аккаунт/кластер\
✓ Балансировать выполнение\
✓ Предотвращать перегрузку операциями

**При hard limit от Telegram:**

- Пауза затронутых операций → пересчёт тайминга → ребаланс → снижение плотности
- Лог события → нейтральное сообщение: "Тайминг выполнения автоматически скорректирован" 
       
- - Система должна учитывать ограничения Telegram и корректно реагировать на них через тайминги, очереди, перераспределение нагрузки и планирование операций.
### 2\.3 Инфраструктурный подход
Каждый актив (аккаунт, бот, канал, группа) — это **ресурс инфраструктуры** с:

- health\_score
- trust\_score
- visibility\_score
- activity\_score
- tags / cluster
- историей операций
- аудитом
- нотами
### 2\.4 Governance (управляемость)
Каждая операция по возможности должна поддерживать:

\- проверки прав

\- approval workflows (для критических действий)

\- audit logs

\- dry-run / preview

\- прогноз тайминга / нагрузки

\- rollback где возможно

Отсутствие одного или нескольких элементов не является причиной

для отказа от реализации новой функции.

Для новых функций допускается поэтапное внедрение governance-механизмов.

-----
## 3\. ТЕХНИЧЕСКАЯ АРХИТЕКТУРА
### 3\.1 Стек

|Компонент|Технология|
| :-: | :-: |
|Bot Framework|aiogram 3.13.1 + Pydantic v2|
|Database|PostgreSQL через asyncpg (Railway)|
|Telegram API|Telethon (userbot) + Bot API|
|Deploy|Railway, Root Dir = /tg-manager, auto-deploy|
|Branch|claude/telegram-bot-services-xfAh6|
### 3\.2 Структура файлов
tg-manager/\
├── main.py                        # точка входа, регистрация роутеров + сервисов\
├── config.py                      # BOT\_TOKEN, DB\_URL, ADMIN\_IDS, ENCRYPTION\_KEY\
├── database/\
│   └── db.py                      # 163+ функций, create\_pool() авто-мигрирует schema\_v\*.sql\
├── bot/\
│   ├── callbacks.py               # ВСЕ CallbackData классы — только здесь\
│   ├── states.py                  # ВСЕ FSMState классы — только здесь\
│   ├── keyboards.py               # shared keyboards (main\_menu, subscription\_locked\_markup)\
│   ├── handlers/                  # 54+ файла обработчиков\
│   │   ├── botmother\_menu.py      # главное меню OS — точка входа /menu\
│   │   ├── start.py               # /start, /help, /version, /cancel\
│   │   ├── accounts.py            # Telegram-аккаунты (QR/phone/session)\
│   │   ├── bots.py                # Bot management\
│   │   ├── channel\_factory.py     # Channel Factory (создать, импорт, bulk, редактировать)\
│   │   ├── channel\_ops.py         # Account Operations (join/leave/post/edit/bulk)\
│   │   ├── group\_factory.py       # Group Factory (создать, импорт, участники)\
│   │   ├── mass\_publish.py        # Mass Publish wizard\
│   │   ├── mass\_ops.py            # Operation Builder / Queue\
│   │   ├── ranking.py             # Search Rankings + Keywords\
│   │   ├── asset\_templates.py     # Шаблоны активов (бот/канал/группа/пост)\
│   │   ├── health\_dashboard.py    # Health Dashboard аккаунтов\
│   │   ├── behavioral\_engine.py   # в services/ — поведенческий анализ\
│   │   └── [40+ других handlers]\
│   └── utils/\
│       ├── op\_helpers.py          # \_acc\_label, \_get\_active\_accounts, \_progress\_bar/text/format\
│       └── subscription.py        # require\_plan(), get\_plan(), locked\_text(), is\_platform\_admin()\
└── services/\
`    `├── account\_manager.py         # ВСЕ Telethon-операции (singleton паттерн)\
`    `├── behavioral\_engine.py       # поведенческие оценки (каждые 15 мин)\
`    `├── session\_simulator.py       # human-like delays (beta-распределение)\
`    `├── account\_monitor.py         # мониторинг ограничений аккаунтов\
`    `├── trust\_engine.py            # обновление trust\_score аккаунтов\
`    `├── shadowban\_monitor.py       # детекция теневых банов\
`    `├── op\_worker.py               # воркер очереди операций\
`    `├── ranking\_checker.py         # периодическая проверка позиций\
`    `├── scheduler.py               # запуск расписаний рассылок\
`    `├── auto\_responder.py          # авто-ответы на сообщения\
`    `├── relay.py                   # live relay диалогов\
`    `├── funnel\_runner.py           # воронки (цепочки шагов)\
`    `├── payment\_checker.py         # проверка платежей\
`    `├── search\_observer.py         # наблюдение за поисковыми паттернами\
`    `├── broadcaster.py             # рассылки\
`    `├── routing\_engine.py          # маршрутизация между ботами\
`    `├── resource\_selector.py       # UNIFIED: единый выбор аккаунтов/прокси (r19)\
`    `├── operation\_bus.py           # UNIFIED: универсальная постановка в очередь (r19)\
`    `├── infra\_memory.py            # UNIFIED: Infrastructure Memory — паттерны успехов (r19, schema\_v65)\
`    `├── infra\_orchestrator.py      # BRAIN: центральный мозг инфраструктуры (r20)\
`    `├── proxy\_selector.py          # UNIFIED: выбор и оценка прокси по infra\_memory (r20)\
`    `├── bot\_api.py                 # Bot API wrapper\
`    `└── railway\_api.py             # Railway API интеграция
### 3\.3 Авто-миграция БД
create\_pool() автоматически выполняет все schema\_v\*.sql в порядке версии. Текущая последняя версия: **v65**

Правило: новая схема → новый файл schema\_v{N+1}.sql в корне tg-manager/.

-----
## 4\. КРИТИЧЕСКИЕ ПАТТЕРНЫ КОДА
### 4\.1 CallbackData — Pydantic v2 (ОБЯЗАТЕЛЬНО)
\# ✅ ПРАВИЛЬНО — Optional для полей которые могут быть пустыми\
class BmCb(CallbackData, prefix="bm"):\
`    `action: str\
`    `sub: Optional[str] = None   # НЕ str = ""\
`    `page: int = 0\
\
\# ❌ НЕПРАВИЛЬНО — ValidationError при десериализации пустых сегментов\
class BmCb(CallbackData, prefix="bm"):\
`    `sub: str = ""               # СЛОМАЕТ кнопки в aiogram 3.13
### 4\.2 Subscription Gate — стандартный паттерн
@router.callback\_query(SomeCb.filter(F.action == "feature"))\
async def cb\_feature(callback: CallbackQuery, pool: asyncpg.Pool) -> None:\
`    `if not await require\_plan(pool, callback.from\_user.id, "starter"):\
`        `await callback.answer()\
`        `await callback.message.edit\_text(\
`            `locked\_text("Название фичи", "starter"),\
`            `reply\_markup=subscription\_locked\_markup("starter"),\
`        `)\
`        `return\
`    `await callback.answer()\
`    `# ... логика
### 4\.3 Telethon — всегда передавать \_acc
\# ✅ ПРАВИЛЬНО — device fingerprint из аккаунта\
result = await account\_manager.some\_operation(\
`    `acc["session\_str"],\
`    `arg1, arg2,\
`    `\_acc=acc,               # dict с полями acc (device\_model, system\_version, app\_version)\
)\
\
\# ❌ НЕПРАВИЛЬНО — один fingerprint на все аккаунты (риск бана)\
result = await account\_manager.some\_operation(acc["session\_str"], arg1)
### 4\.4 Общие хелперы — только из op\_helpers
\# ✅ ПРАВИЛЬНО\
from bot.utils.op\_helpers import (\
`    `\_acc\_label, \_get\_active\_accounts,\
`    `\_progress\_bar, \_progress\_text, \_format\_duration,\
)\
\
\# ❌ НЕПРАВИЛЬНО — НЕ определять эти функции локально в handler-файлах\
def \_acc\_label(acc): ...       # ДУБЛИКАТ — удалить
### 4\.5 FSM Template Prefill паттерн
\# В asset\_templates.py при применении шаблона:\
await state.update\_data(tpl\_prefill={\
`    `"title": "...", "description": "...", "username": "...",\
})\
await state.set\_state(ChannelFactoryFSM.choosing\_account)\
\
\# В account\_chosen handler — проверить prefill и пропустить FSM-шаги:\
sd = await state.get\_data()\
prefill = sd.get("tpl\_prefill") or {}\
if prefill.get("title"):\
`    `await state.update\_data(\
`        `title=prefill["title"],\
`        `about=prefill.get("description", ""),\
`        `tpl\_prefill=None,\
`    `)\
`    `await \_show\_chanf\_cluster\_or\_confirm(callback, state, pool)\
`    `return
### 4\.6 Anti-ban задержки — умный подход
\# channel\_factory.py bulk create — ТЕКУЩИЙ ПАТТЕРН\
from services import session\_simulator\
\
for i, item in enumerate(items):\
`    `await session\_simulator.typing\_delay(item\_name)  # перед действием\
`    `# ... выполнить операцию ...\
`    `if i < len(items) - 1:\
`        `if i % 5 == 0:  # каждые 5 — длинная пауза\
`            `delay = random.uniform(300, 600) \* session\_simulator.chaos\_factor()\
`        `else:\
`            `delay = random.uniform(45, 90) \* session\_simulator.chaos\_factor()\
`        `await asyncio.sleep(delay)\
\
\# channel\_ops.py bulk create — ТЕКУЩИЙ ПАТТЕРН\
chaos = session\_simulator.chaos\_factor()\
await asyncio.sleep(max(backoff, flood, base\_delay \* chaos))\
\# Каждые 5 операций: base\_delay = 120.0 (cooldown)
### 4\.7 Import существующих активов (NEW паттерн)
\# Получить каналы из аккаунта и сохранить в managed\_channels\
from services import account\_manager\
from database.db import upsert\_managed\_channels\
\
dialogs = await account\_manager.get\_dialogs(acc["session\_str"], limit=200, \_acc=acc) or []\
channels = [d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")]\
await upsert\_managed\_channels(pool, owner\_id, acc["id"], channels)
### 4\.8 Безопасный SQL — всегда параметризованные запросы
\# ✅ ПРАВИЛЬНО\
await pool.execute("UPDATE tg WHERE id=$1 AND owner\_id=$2", item\_id, user\_id)\
\
\# ❌ НЕПРАВИЛЬНО — SQL-инъекция\
await pool.execute(f"UPDATE tg WHERE id={item\_id}")

-----
## 5\. ВСЕ CALLBACK-ПРЕФИКСЫ (57 штук, все уникальны)
bot  edit  aud  wh   bc   bulk  cmd  tpl  sch  mg\
ar   rl    fn   st   note sw    crm  au   exp  dl\
eng  seo   net  cl   sub  ai    nbc  acc  rank chan\
cinv ref   bm   atpl grpf mop   btf  chanf mpub comp\
vis  hlth  prx  clm  gp   tba   lib  prs  wu   infra\
cln  dm

**Правило:** новый CallbackData → новый уникальный prefix в bot/callbacks.py.

-----
## 6\. ЗАРЕГИСТРИРОВАННЫЕ РОУТЕРЫ В main.py (54 штуки)
Порядок регистрации важен — более специфичные роутеры раньше:

1. bm\_handler (botmother\_menu) — первый
1. Factory handlers: bot\_factory, group\_factory, chan\_factory
1. Operation handlers: mass\_ops, asset\_tpl, mass\_pub, competitors
1. Sub/billing: sub\_handler
1. Core: start, bots, edit, audience, webhooks, broadcast, etc.
1. relay\_handler — перед последними (ловит F.reply\_to\_message)
1. admin\_handler — самый последний
-----
## 7\. ФОНОВЫЕ СЕРВИСЫ (18 штук в main.py + 2 библиотеки)
asyncio.create\_task(deploy\_notifier.notify\_deploy(pool, bot))\
asyncio.create\_task(scheduler.run(pool, http))\
asyncio.create\_task(auto\_responder.run(pool, http, bot))\
asyncio.create\_task(relay\_service.run(pool, http))\
asyncio.create\_task(funnel\_runner.run(pool, http))\
asyncio.create\_task(payment\_checker.run(pool, http, bot))\
asyncio.create\_task(ranking\_checker.run(pool, bot))\
asyncio.create\_task(search\_observer.run\_confirmation\_loop(pool, bot))\
asyncio.create\_task(account\_monitor.run(pool, bot))\
asyncio.create\_task(trust\_engine.run(pool, bot))\
asyncio.create\_task(shadowban\_monitor.run(pool, bot))\
asyncio.create\_task(op\_worker.run(pool, bot))\
asyncio.create\_task(behavioral\_engine.run(pool, bot))\
asyncio.create\_task(account\_warmer.run\_warmup\_loop(pool))\
asyncio.create\_task(account\_health.run\_health\_check\_loop(pool))\
asyncio.create\_task(payment\_webhook.run(pool, bot))  # HTTP :8080\
asyncio.create\_task(task\_registry.run\_cleanup\_loop())\
asyncio.create\_task(drift\_detector.run(pool, bot))

**Библиотеки (не фоновые сервисы — используются через прямые вызовы):**

- services/flood\_engine.py — FloodWait tracking, adaptive pacing, get\_best\_account() (используется op\_worker, infra\_analytics)
- services/session\_pool.py — Session lifecycle, warm/load API (импортируется по необходимости)
-----
## 8\. АНТИ-БАН СИСТЕМА
### Device Fingerprints
- 20 уникальных Android-профилей в account\_manager.py:\_ANDROID\_DEVICES
- Каждый аккаунт имеет device\_model, system\_version, app\_version в БД (schema\_v23)
- generate\_device\_fingerprint() → рандомный профиль при создании
- \_make\_client(session\_str, \_acc) → использует сохранённый профиль аккаунта
### Flood Protection
- Exponential backoff: \_backoff(attempt, base=2.0, cap=60.0)
- Flood wait respect: asyncio.sleep(max(backoff, flood\_seconds))
- session\_simulator.chaos\_factor() — мультипликатор 0.7–1.3 для рандомизации
- session\_simulator.typing\_delay(text) — пауза перед каждым действием
- Batch паузы: каждые 5 операций → длинный cooldown 300-600s
### Account Trust System
- trust\_engine → обновляет trust\_score каждого аккаунта
- account\_monitor → следит за ограничениями
- shadowban\_monitor → детектирует теневые баны
- Операции используют аккаунты с наивысшим trust\_score (ORDER BY trust\_score DESC NULLS LAST)
- При бане: deactivate\_account() + record\_flood\_event()
### Session Simulator (services/session\_simulator.py)
human\_delay(1.5, 8.0)       # beta-распределение — не равномерное\
short\_pause(0.3, 1.5)        # быстрая пауза\
typing\_delay(text)            # симуляция набора текста\
bulk\_item\_pause(i, 10)       # длинная пауза каждые 10 элементов\
chaos\_factor(base, spread)   # мультипликатор для рандомизации
### Behavioral Engine (services/behavioral\_engine.py)
Собирает поведенческие события → вычисляет каждые 15 мин:

- attention\_score — устойчивость внимания (0–100)
- habit\_score — привычность, регулярность по неделям (0–100)
- ecosystem\_score — встроенность (cross-nav links) (0–100)
- decay\_rate — скорость угасания per day
-----
## 9\. СХЕМА БД (60+ таблиц, v47 schema)
-- Инфраструктура\
tg\_accounts      (id, owner\_id, phone, session\_str, device\_model, system\_version, app\_version,\
`                  `trust\_score, flood\_count\_7d, cooldown\_until, last\_flood\_at, is\_active, cluster)\
managed\_bots     (bot\_id, added\_by, token\_encrypted, username, first\_name, is\_active)\
managed\_channels (owner\_id, acc\_id, channel\_id, title, username, access\_hash)\
clusters         (id, owner\_id, name, created\_at)\
\
-- Видимость\
tracked\_keywords  (id, bot\_id, owner\_id, keyword, is\_active)\
search\_rankings   (id, keyword\_id, position, checked\_at)\
search\_snapshots  (id, keyword\_id, raw\_json, checked\_at)\
search\_memory     (owner\_id, keyword, search\_count, affinity\_score)\
\
-- Операции\
operation\_queue  (id, owner\_id, op\_type, status, params, total\_items, done\_items,\
`                  `scheduled\_for, template\_id, created\_at)\
operation\_log    (id, op\_id, step\_num, target, status, message)\
\
-- Уведомления и алерты\
restriction\_events (id, owner\_id, account\_id, bot\_id, event\_type, severity, details, created\_at)\
account\_flood\_log  (id, account\_id, operation, flood\_seconds, created\_at)\
notification\_settings (user\_id, new\_user, flood\_warning, position\_change, op\_complete, restriction)\
\
-- Поведенческий слой\
behavioral\_events        (id, owner\_id, entity\_type, entity\_id, event\_type, meta, occurred\_at)\
entity\_behavioral\_score  (owner\_id, entity\_type, entity\_id, attention\_score, habit\_score,\
`                          `ecosystem\_score, decay\_rate, reentry\_count, updated\_at)\
\
-- Пользователи и монетизация\
platform\_users   (user\_id, username, first\_name, last\_active, created\_at)\
users            (id, username, plan, plan\_until)\
platform\_referral\_codes (user\_id, code, created\_at)\
platform\_referrals      (referrer\_id, referred\_id, activated\_at, paid\_at)\
referral\_rewards        (user\_id, level, plan, days, given\_at)\
\
-- Боты-специфика (per bot)\
bot\_users, broadcasts, scheduled\_broadcasts, templates, auto\_replies,\
funnel\_sequences, funnel\_subscribers, relay\_sessions, relay\_messages,\
crm\_contacts, crm\_tags, crm\_notes, ab\_experiments, deep\_links, etc.

-----
## 10\. ПОДПИСКИ

|Тир|Цена|Лимит ботов|Лимит аккаунтов|Ключевые фичи|
| :-: | :-: | :-: | :-: | :-: |
|free|$0|3|2|Базовые операции, алерты, уведомления|
|starter|$9/мес|10|5|Расписание, отчёты, публикация, поисковая память|
|pro|$25/мес|30|15|Создание каналов/групп, behavioral dashboard, кластеры|
|enterprise|$69/мес|∞|∞|Всё, + свармы, эксперименты, мультигео|

**Subscription gate паттерн:**

from bot.utils.subscription import require\_plan, locked\_text\
from bot.keyboards import subscription\_locked\_markup\
\
if not await require\_plan(pool, callback.from\_user.id, "starter"):\
`    `await \_edit(callback, locked\_text("Название", "starter"), subscription\_locked\_markup("starter"))\
`    `return

-----
## 11\. BOTMOTHER OS — КАРТА МЕНЮ (актуальная)
/menu → BotMother OS\
├── 🏗️ Infrastructure\
│   ├── 📱 Аккаунты           → AccCb(action="menu")\
│   ├── 🤖 Мои боты           → BotCb(action="list")\
│   ├── 📡 Каналы & операции  → ChanCb(action="menu")\
│   │   └── ChanFactCb(action="menu")\
│   │       ├── ➕ Создать канал\
│   │       ├── 📋 Массовое создание\
│   │       ├── 📥 Импорт из Telegram\
│   │       ├── ✏️ Редактировать\
│   │       ├── 📤 Массовая публикация\
│   │       ├── 📊 Статистика\
│   │       └── 🔗 Генерация ссылок\
│   ├── 👥 Группы             → GroupFCb(action="menu")\
│   │   ├── ➕ Создать группу\
│   │   ├── 📥 Импорт из Telegram\
│   │   ├── 📋 Мои группы\
│   │   ├── 👥 Участники\
│   │   └── 📢 Объявление\
│   ├── 🔗 Кластеры           → ClustMCb(action="menu")\
│   ├── 🌐 Прокси             → ProxyCb(action="menu")\
│   ├── 🌡 Разогрев           → WarmupCb(action="menu")       ← r10\
│   ├── 🔍 Парсер             → AudienceCb(action="menu")     ← r10\
│   └── ❤️ Здоровье           → HealthCb(action="menu")\
├── 👁️ Visibility\
│   ├── 🔍 Ключевые слова     → pick\_bot\_for → RankCb(action="menu")\
│   ├── 📊 Позиции            → pick\_bot\_for → RankCb(action="menu")\
│   ├── 🏆 Конкуренты         → CompCb(action="menu")\
│   ├── 🔔 Алерты             → BmCb(action="alerts") [FREE]\
│   └── 📋 Отчёты             → BmCb(action="vis\_reports") [STARTER]\
├── ⚙️ Operations\
│   ├── ⚡ Массовые действия  → BmCb(action="bulk\_ops")\
│   │   ├── 🤖 Боты           → NetworkCb(action="menu")\
│   │   ├── 📡 Каналы bulk    → ChanCb(action="bulk\_menu")\
│   │   ├── 📤 Публикация     → MassPubCb(action="menu")\
│   │   └── 📱 Аккаунты bulk  → ChanCb(action="bulk\_menu")\
│   ├── 🛠️ Построитель        → MassOpCb(action="menu")\
│   ├── 📋 Очередь            → MassOpCb(action="queue")\
│   ├── ⏱️ Планировщик        → BmCb(action="op\_planner")\
│   ├── 📄 Шаблоны            → AssetTplCb(action="menu")\
│   ├── 🌍 Global Presence    → GeoPresenceCb(action="menu")  ← r11\
│   ├── 📊 Отчёты             → BmCb(action="op\_reports") [STARTER]\
│   └── 📋 Active Tasks       → BmCb(action="active\_tasks")   ← r12\
├── 📢 Broadcasts\
│   ├── 📢 Рассылка по боту   → BotCb(action="list")\
│   ├── 🌐 Сетевая рассылка   → NetBcCb(action="choose\_target")\
│   └── 📅 Расписание         → BmCb(action="schedules")\
├── 💬 Inbox / Relay          → pick\_bot\_for → RelayCb(action="menu")\
├── 🤖 AI Assistant           → AiCb(action="start")\
├── 🧠 Аналитика	→ BmCb(action="behavioral") [PRO]\
│   ├── 📊 Топ по вниманию\
│   ├── 🔄 Активные привычки\
│   ├── 📉 Угасающие ресурсы\
│   ├── 🌐 Экосистемные узлы\
│   └── 🔍 Поисковая память\
├── 💳 Billing                → SubCb(action="menu")\
├── 👥 Referral               → RefCb(action="menu")\
└── ⚙️ Settings\
`    `├── 📢 Авто-ответы         → pick\_bot\_for → AutoReplyCb(action="list")\
`    `└── 🔔 Уведомления         → BmCb(action="notifications") [FREE]

-----
## 12\. ИНВЕНТАРЬ ФИЧ (актуальный статус)
### ✅ Инфраструктура — ГОТОВО
- Multi-account management (QR/номер/session/import/CSV)
- Device fingerprints per account (20 Android-профилей, schema\_v23)
- **Импорт существующих каналов** из Telegram в managed\_channels
- **Импорт существующих групп** из Telegram в managed\_channels
- Bot management (добавить, токен, команды, webhooks, multigeo)
- Channel Factory (создать, bulk-создать, импорт, редактировать, статистика, ссылки)
- Group Factory (создать, импорт, список, участники, объявление)
- Cluster Manager (группировка каналов/ботов)
- Proxy Manager (socks5, проверка, привязка)
- Health Dashboard V2 (health\_score тренды, авто-ротация, schema\_v45)
- **Topology Map** — граф связей ботов/каналов/аккаунтов (NEW r15)
- **Drift Detection** — мониторинг изменений каналов (NEW r15, schema\_v46)
- **Presence Pack System** — воронка бот+каналы+группы (NEW r15, schema\_v47)
- **CSV Import Center** — батч-импорт аккаунтов + txt для DM/Invite (NEW r15)
- **Deploy Notifier** — Telegram-уведомления о деплое (NEW r15)
### ✅ Операции — ГОТОВО
- Mass Ops (bulk edit bots, bulk join/leave)
- Operation Queue (очередь, прогресс, отмена)
- Mass Publish (все каналы / по аккаунту / dry-run + **Умный тайминг** 30-90s)
- Network Broadcast (рассылка по сети ботов)
- Asset Templates + Apply Template (100%)
- Channel Operations (join/leave/publish/edit/contacts — полный набор)
- **Умные тайминги в bulk create**: typing\_delay + 45-90s chaos + 5-10 мин каждые 5
- **Strike Engine V2** — эшелонированная атака, 12 векторов (NEW r15)
- **Preview/Confirm** для bulk операций (NEW r15)
- **Функциональные авто-ответы** на команды в шаблонах ботов (NEW r15)
### ✅ Видимость — ГОТОВО
- Search Rankings (трекинг позиций)
- Search Observations (паттерн подтверждения)
- Competitors (мониторинг конкурентов)
- Visibility Reports [STARTER]
- Alerts (агрегация restriction\_events) [FREE/STARTER]
### ✅ Поведенческий слой — ГОТОВО
- Behavioral Events log
- Behavioral Engine (attention/habit/ecosystem/decay каждые 15 мин)
- Search Memory (keyword affinity)
- Behavioral Dashboard [PRO]
- Session Simulator (интегрирован в channel\_factory + channel\_ops)
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
- Trust Engine (scoring + авто-ротация)
- Shadowban Monitor
- Operation Reports [STARTER]
- Drift Detection (изменения username/title/about, алерты)
- Deploy Notifier (Railway webhook → Telegram)
### ✅ UX-улучшения — ГОТОВО
- Описания всех разделов в BotMother OS меню
- Онбординг с тремя сценариями для новых пользователей
- Статус-иконки ✅/⛔ в списке аккаунтов
- Описания в Channel Factory, Group Factory, Mass Publish
- Описания в Visibility, Operations, Broadcasts, Inbox, Settings
- Кнопка Релог — переподключение аккаунта без повторного ввода номера
- Back-кнопки на всех lock-screen экранах подписки
- Cancel/Back во всех FSM wizard'ах (полный UX-аудит r15)
- Валидация ввода во всех FSM wizard'ах (r15)
### ✅ Надёжность и мониторинг — ГОТОВО (r12)
- Live task tracking and cancellation system
- Active Tasks button в главном меню + /tasks keyboard
- Background mass\_publish с task\_registry (не блокирует интерфейс)
- Telethon operation timeouts (120s, предотвращение зависаний)
- Resilient service restart (factory pattern — не coroutine reuse)
- DM campaign task registration + fix cancellation propagation
- SQL injection fixes, silent fails, service auto-restart
- AuthKeyUnregistered detection + удаление мёртвых сессий
### ✅ Account Infrastructure Contract — ГОТОВО (r17)
- **Tags/Pools** — теги и пулы для аккаунтов (schema\_v60), умная фильтрация в ops
- **Pressure Score** — Infrastructure Pressure 0-100 (services/infra\_pressure.py)
- **Account CRM** — labels, warnings, project на аккаунте
- **Proxy Intelligence** — proxy\_quality\_log, статистика success/failure в UI
- **Auto-Rebalancing** — smart distribution: primary/monitoring/cooldown (infra\_analytics)
- **Disaster Recovery** — просмотр активов аккаунта перед удалением
- **Autonomous Recommendations** — 8-правильный советник (services/infra\_advisor.py)
- **Pool filter в Mass Ops** — выполнение операций только по пулу аккаунтов
- **Health Dashboard обновлён** — пулы, теги в списке аккаунтов + Pressure Score
- **Free Mode** — глобальный бесплатный доступ + toggle в /admin (schema\_v59)
### ✅ AI, память и провайдеры — ГОТОВО (r18)
- **AI Memory System** — /remember, /memory, /forget команды + auto-save [MEMORY:] тегов (schema\_v63)
- **AI Provider Failover** — OpenRouter, Groq, Gemini failover (services/ai\_providers.py)
- **AI 📚 Память кнопка** — просмотр/удаление записей памяти из чата
- **Broadcast Stats** — детальная статистика рассылки (delivery%, failed, pending) + 📊 кнопка
- **Quick Post Save Template** — кнопка 💾 на шаге подтверждения сохраняет пост в шаблоны
- **Op Queue Retry/Clear** — 🔁 повтор и 🗑 очистка завершённых операций в очереди
- **Warmup Level** — отслеживание уровня прогрева (light/medium/deep) в БД (schema\_v64)
- **Per-owner Semaphore** — max 3 параллельных операции на владельца (op\_worker.py)
- **Scheduler Missed Fix** — рассылки с опозданием >1ч помечаются 'missed', не запускаются
- **Admin Error Counter** — кнопка 🐛 показывает число новых отчётов об ошибках
### ✅ UX и надёжность — ГОТОВО (r17)
- Кнопки ◀️ Назад добавлены во все оставшиеся dead-end экраны (30+ файлов)
- ❌ Отмена во всех FSM prompt'ах (bulk, network\_bulk, crm, schedule, templates и др.)
- op\_detail: прогресс-бар [████░░] + ETA + elapsed time
- op\_detail: кнопка 🛑 Отменить операцию (running/pending)
- op\_worker: progress monitor — уведомления 25/50/75% (фоновая корутина)
- Warmup Log — детальный лог действий по дням с метками (📖 читал, 🔔 вступил...)
- FSM input validation в crm, workspaces, deeplinks и других handlers
- Account Cleaner: защита аккаунтов с активными каналами/операциями
- Deploy Notifier: статистика платформы (аккаунты, боты, pressure) при деплое
- Infra Analytics: Pressure Score + пулы + качество прокси + авто-балансировка
-----
## 13\. GAP-АНАЛИЗ (все критические пробелы закрыты — r17)
### ✅ ВСЕ КРИТИЧЕСКИЕ ПРОБЕЛЫ ЗАКРЫТЫ (r12-r15)

|Функция|Статус|Файл|
| :-: | :-: | :-: |
|Operation Planner FSM|✅|botmother\_menu.py|
|record\_reentry в start.py|✅|start.py|
|record\_cross\_nav при навигации|✅|botmother\_menu.py|
|Notification delivery|✅|db.notify\_if\_enabled → account\_monitor/ranking|
|Post template prefill|✅|mass\_publish.py|
|DM-кампании|✅|dm\_campaigns.py + dm\_engine.py|
|Retry Intelligence|✅|op\_worker.py \_classify\_op\_error + \_maybe\_requeue|
|Invite Distribution Engine|✅|services/invite\_engine.py|
|Geo Router|✅|services/geo\_router.py|
|Capacity Planner|✅|services/capacity\_planner.py|
|Admin bulk tools|✅|admin.py (bulk grant + cleanup + platform ops)|
|Payment Webhook|✅|services/payment\_webhook.py (port 8080)|
|Funnel referral conversions|✅|funnel\_runner.py|
|Session Converter|✅|services/session\_converter.py|
|Account Cleaner|✅|services/account\_cleaner.py + handler|
|Strike Module|✅|strike.py (12-векторная атака + $250 lifetime)|
|Enterprise Tier|✅|self-healing schema loader + все продвинутые фичи|
|Global Presence Factory V1+V2+V3|✅|global\_presence.py + geo\_data + username\_engine|
|Flood Intelligence Engine|✅|services/flood\_engine.py (library — used by op\_worker, infra\_analytics)|
|Session Orchestrator|✅|services/session\_pool.py (library — session lifecycle, warm/load API)|
|Account Health Engine V2|✅|services/account\_health.py + schema\_v45|
|Audience Parser|✅|services/parser.py + audience\_parser.py|
|Account Warming|✅|services/account\_warmer.py + handler|
|Live Task Tracking|✅|services/task\_registry.py + active\_tasks.py (r12)|
|Telethon Timeouts|✅|account\_manager.py (120s default, r12)|
|Resilient Service Restart|✅|main.py factory pattern (r12)|
|Back buttons on lock screens|✅|all subscription gates (r11)|
|A/B auto-completion|✅|experiments.py p-value < 0.05 (r11)|
|Visibility CSV export|✅|ranking.py (r11)|
|Search Memory drill-down|✅|ranking.py (r11)|
|CSV Import Center|✅|CSV import аккаунтов + txt для DM/Invite (r15)|
|Drift Detection|✅|schema\_v46 + drift\_monitor (r15)|
|Topology Map|✅|граф связей ботов/каналов/аккаунтов (r15)|
|Strike Engine V2|✅|эшелонированная атака 12 векторов (r15)|
|Presence Pack System|✅|schema\_v47 + funnel logic (r15)|
|Deploy Notifier|✅|Railway webhook → Telegram (r15)|
|UX Audit (Cancel/Back)|✅|все FSM wizard'ы (r15)|
### 🟢 Низкий приоритет (nice to have)

|Пробел|Описание|
| :-: | :-: |
|Telegram Mini App|Веб-дашборд для таблиц/графиков (опционально)|
|Unified Asset Registry UI|Единый список всех активов с фильтрами|
|RBAC / Multi-user workspaces|Несколько пользователей в одной организации|
|Approval workflows|Подтверждение перед критическими bulk-операциями|
|Topology map|Граф связей ботов/каналов/аккаунтов|

-----
## 14\. ИЗВЕСТНЫЕ ЛОВУШКИ

|Ловушка|Решение|
| :-: | :-: |
|str = "" в CallbackData → ValidationError|Всегда Optional[str] = None|
|Два handler на один prefix+action|Первый зарегистрированный побеждает тихо|
|asyncio.sleep(0.5) в bulk → флуд-бан|Использовать session\_simulator|
|Telethon без \_acc → один fingerprint|Всегда передавать \_acc=acc|
|\_progress\_text дублируется в файлах|Только из op\_helpers, кастомный title через параметр|
|add\_tracked\_keyword без behavioral|После → record\_search\_repeat|
|ScheduleCb(bot\_id=0) → бот не найден|Показывать bot-picker перед переходом|
|f-string SQL → инъекция|ТОЛЬКО параметры $1, $2 asyncpg|
|message.edit\_text без callback.answer()|Всегда await callback.answer() первым|
|flood\_wait\_until → не существует|Правильная колонка: cooldown\_until (schema\_v24)|
|account\_flood\_log.owner\_id → нет поля|JOIN с tg\_accounts для получения owner\_id|

-----
## 15\. РАБОЧИЙ ПРОЦЕСС
### Стандартная итерация
\# 1. Убедиться в ветке\
git branch  # должна быть claude/telegram-bot-services-xfAh6\
\
\# 2. Сделать изменения, проверить синтаксис ВСЕХ изменённых файлов\
python3 -c "\
import ast, sys\
files = ['path/to/file.py']\
for f in files:\
`    `try:\
`        `ast.parse(open(f).read())\
`    `except SyntaxError as e:\
`        `print(f'ERROR {f}: {e}'); sys.exit(1)\
print('All OK')\
"\
\
\# 3. Если новая БД-таблица → создать schema\_v{N+1}.sql\
\# create\_pool() подхватит автоматически при деплое\
\
\# 4. Коммит + пуш (stop-hook требует пуш)\
git add tg-manager/path/to/file.py\
git commit -m "feat/fix/refactor: краткое описание"\
git push -u origin claude/telegram-bot-services-xfAh6
### Добавление нового handler-файла
\# 1. Создать bot/handlers/my\_feature.py с router = Router()\
\# 2. Добавить в main.py (в нужное место по приоритету):\
from bot.handlers import my\_feature as my\_feature\_handler\
dp.include\_router(my\_feature\_handler.router)\
\# 3. Новый CallbackData → добавить в bot/callbacks.py (уникальный prefix)\
\# 4. Новые FSM states → добавить в bot/states.py
### Добавление нового фонового сервиса
\# 1. Создать services/my\_service.py с async def run(pool):\
\# 2. В main.py:\
from services import my\_service\
asyncio.create\_task(my\_service.run(pool))

-----
## 16\. СЛЕДУЮЩИЕ ПРИОРИТЕТЫ (r15 → r16)
### ✅ ЗАКРЫТО (r14)
1. ✅ **Account Health Dashboard V2** — health\_score тренды + улучшенные рекомендации
   1. schema\_v45: account\_health\_history для снапшотов health\_score
   1. health\_score в списке аккаунтов + тренд за 7 дней
   1. Контекстные рекомендации с health\_tips
   1. Персистентность health\_score в account\_health.py
1. ✅ **Авто-ротация аккаунтов** — автоматические кулдауны
   1. trust < 0.3 → 72h, trust 0.3–0.6 → 24h
   1. Фоновый цикл в trust\_engine каждые 6 часов
   1. Уведомления владельцам через notify\_if\_enabled
1. ✅ **Behavioral Engine Enhancement** — anomaly detection v2
   1. Velocity anomaly: события/час > 3× от среднего за 7 дней
   1. Pattern deviation: attention/ecosystem отклонение > 50% от baseline
1. ✅ **Global Presence Factory V3** — полный пакет (каналы + группы + боты)
### ✅ ЗАКРЫТО (r15)
5. ✅ **Import Center** — CSV import для bulk операций
   1. Массовый импорт аккаунтов из CSV (колонки session, cluster)
   1. Auto-cluster assignment при импорте
   1. Загрузка .txt для Bulk DM и InviteUsers
5. ✅ **Drift Detection** — мониторинг изменений
   1. Отслеживание изменений username/title/about в каналах (schema\_v46)
   1. Алерты при неожиданных изменениях в restriction\_events
5. ✅ **Topology Map** — граф связей
   1. Визуализация связей ботов/каналов/аккаунтов
   1. Статистика перекрёстных ссылок
### ✅ ДОПОЛНИТЕЛЬНО:
- ✅ **Strike Engine V2** — эшелонированная атака (12 векторов, pre-flight, staggered waves)
- ✅ **Presence Pack System** — воронка бот+каналы+группы (schema\_v47)
- ✅ **Deploy Notifier** — Telegram-уведомления о деплое через Railway webhook
- ✅ **Функциональные авто-ответы** на команды во всех шаблонах ботов
- ✅ **Bot Admin Sessions** — /admin TOKEN для владельцев ботов (schema\_v47)
- ✅ **UX audit** — Cancel/Back кнопки во всех FSM wizard'ах + валидация ввода
### 🟡 Ожидают определения
8\. Новые функции и массовые операции могут реализовываться по прямому запросу пользователя даже при отсутствии их в текущем roadmap.

-----
## 17\. ДЕПЛОЙ
- **Платформа:** Railway
- **Root Directory:** /tg-manager
- **Ветка:** claude/telegram-bot-services-xfAh6 → auto-deploy при пуше
- **Build:** pip install -r requirements.txt && python main.py
- **Проверка после деплоя:** /version или /menu в боте
- **Текущая build:** 2026.06.03-r25
- **Логи:** Railway dashboard → Deployments → Latest
-----
## 18\. ПРИНЦИПЫ UX (для Telegram-native интерфейса)
1. **Каждое меню** должно начинаться с краткого описания что это такое и зачем
1. **Каждая кнопка** должна быть понятна без объяснений — emoji + понятное название
1. **Сложные операции** → пошаговый FSM wizard с возможностью отмены на каждом шаге
1. **Bulk-операции** → всегда показывать прогресс + итог
1. **Ошибки** → конкретное описание что не так и как исправить
1. **Новые пользователи** → онбординг с тремя сценариями использования
1. **Subscription gates** → объяснять ЧТО закрыто и предлагать апгрейд
1. **Деструктивные действия** → подтверждение с preview перед выполнением
1. **Пустые состояния** → объяснять что добавить и как это сделать
1. **Возврат назад** → кнопка "◀️ Назад" всегда должна быть на каждом экране
-----
### ✅ ЗАКРЫТО (r17)
- ✅ **Account Infrastructure Contract** — 7 систем (Tags/Pools, Pressure, CRM, Proxy Intel, Auto-Rebalancing, Disaster Recovery, Autonomous Recommendations)
- ✅ **Back buttons UX audit** — 30+ файлов, мёртвые экраны устранены полностью
- ✅ **FSM cancel validation** — все wizard'ы имеют ❌ Отмена на каждом шаге
- ✅ **op\_worker progress monitor** — уведомления 25/50/75% milestones
- ✅ **op\_detail UX** — прогресс-бар, ETA, elapsed time, кнопка отмены
- ✅ **Warmup action log** — детальный лог по дням с типами действий
- ✅ **Pool filter in Mass Ops** — операции по пулу аккаунтов
- ✅ **Free Mode** — глобальный toggle в /admin + platform\_settings таблица
- ✅ **Strike reliability** — FloodWait persistence в БД, cooldown filtering
- ✅ **FSM state bug fix** — OpBuilderFSM.confirming, дублирующие State()
- ✅ **account\_warmer crash fix** — None session\_str early return

### ✅ ЗАКРЫТО (r19) — BOTMOTHER ЕДИНЫЙ ОРГАНИЗМ
- ✅ **resource\_selector.py** — единый выбор аккаунтов: select\_account/select\_accounts/select\_for\_wave, обёртка flood\_engine
- ✅ **operation\_bus.py** — OP\_REGISTRY (10 op\_type) + submit/cancel/get\_status/list\_active/list\_recent API
- ✅ **infra\_memory.py** — Infrastructure Memory: паттерны успехов/ошибок, memory\_score, proxy\_score, best\_hour, flush loop
- ✅ **schema\_v65** — infra\_memory\_accounts + infra\_memory\_proxies таблицы
- ✅ **Strike в op\_worker** — op\_type="strike" → _exec_strike() → staggered\_strike() + progress callback
- ✅ **preflight\_accounts** — composite sort: trust + memory\_score − risk\_score, flood\_engine in-memory cooldown
- ✅ **infra\_memory wiring** — strike\_engine и op\_worker записывают success/fail в память после операций
- ✅ **Security hardening** — try-except для int() конвертаций params, timestamp > 0 check, done\_items=0 explicit

### ✅ ЗАКРЫТО (r20) — BOTMOTHER EPOCH I: ФУНДАМЕНТ

- ✅ **backoff() консолидация** — 4 дублирующих _backoff() → один канонический backoff() в op_helpers.py
- ✅ **extract_flood_wait консолидация** — дубль в op_worker удалён, использует op_helpers.extract_flood_wait
- ✅ **Мёртвый код account_manager** — _backoff() и _extract_flood_wait() (never called) удалены
- ✅ **resource_selector.include_ids** — select_all_active() получил параметр для фильтрации по ID
- ✅ **resource_selector в op_worker** — _exec_mass_publish, _exec_bulk_join, _exec_bulk_leave → resource_selector.select_all_active()
- ✅ **infra_memory per-item** — record_account_op() добавлен в publish/join/leave на каждое действие
- ✅ **infra_orchestrator.py** — центральный мозг: get_state(), recommend_accounts(), estimate_capacity(), is_ready_for_op()
- ✅ **global_presence_package в OP_REGISTRY** — отсутствующий тип добавлен
- ✅ **global_presence operation_bus** — 4 прямых INSERT → operation_bus.submit()
- ✅ **botmother_menu operation_bus** — 1 прямой INSERT → operation_bus.submit()
- ✅ **mass_ops operation_bus** — 3 прямых INSERT → operation_bus.submit()
- ✅ **ai_tools operation_bus** — 1 прямой INSERT → operation_bus.submit()
- ✅ **proxy_selector.py** — Phase 6: get_proxy_score, record_proxy_result, rank_accounts_by_proxy_quality
- ✅ **_normalize_result()** — унифицированный формат результата операций: ok/failed/total/summary/duration_s/op_type

### ✅ ЗАКРЫТО (r21) — EPOCH I: PRESSURE GATE

- ✅ **get_pressure_warning()** — мягкое предупреждение (≥70) в infra_orchestrator.py
- ✅ **pressure gate в bulk_join/bulk_leave confirm** — 🚫 хардблок при ≥85, ⚠️ тост при 70–84
- ✅ **pressure gate в mass_publish confirm** — та же логика, infra_orchestrator влияет на UX
- ✅ **pressure gate в ob_confirm (Operation Builder)** — первая точка где "мозг" реально управляет действиями

### ✅ ЗАКРЫТО (r22) — BOTMOTHER EPOCH II: INFRASTRUCTURE INTELLIGENCE CONTRACT

- ✅ **intelligence_engine.py HTML fix** — html.escape() в format_pre_launch_block() (risk.reasons, warning_text, go_reason)
- ✅ **intelligence_engine bug fix** — исправлен мёртвый if/else в flood_risk_component (обе ветки были идентичны)
- ✅ **available_accs SQL fix** — excluded spamblock/banned/deactivated/session_expired из подсчёта доступных аккаунтов в assess_risk
- ✅ **Cooldown Reset UI** — кнопка "🔓 Сбросить кулдауны" в Health Dashboard; reset_one + reset_all handlers
- ✅ **Pool Pressure Score** — compute_pool_pressure() → per-pool давление с cooldown+restriction+flood; format_pool_pressure()
- ✅ **Pressure Panel → Pool Breakdown** — панель давления показывает breakdown по пулам (≥2 пулов)
- ✅ **Enhanced Advisor (13 правил, было 8)** — правила 9-13: memory_performance, pool_concentration, op_failure_spike, no_proxy, idle_high_trust
- ✅ **Copilot Memory Analyzer** — _analyze_memory_performance(): chronic underperformers + trust-memory divergence
- ✅ **Copilot Timing Analyzer** — _analyze_timing_patterns(): best operation hour из 14d history в operation_queue
- ✅ **Intelligence Dashboard** — InfraCb(action="intelligence"): top-3 accounts, risk for join/publish/strike, proxy quality
- ✅ **format_pre_launch_block** — добавлены исключённые аккаунты с причинами (макс. 3): "↳ acc_name: причина исключения"
- ✅ **Strike Intelligence upgrade** — get_pre_launch_intelligence() + format_pre_launch_block() с fallback на infra_orchestrator
- ✅ **Mass Ops Intelligence upgrade** — _intel_block() → intelligence_engine primary + infra_orchestrator fallback
- ✅ **Memory Feedback Loop completeness** — global_presence_channel + global_presence_bot теперь пишут в infra_memory per-item

### ✅ ЗАКРЫТО (r23) — EPOCH II: LEARNING ENGINE + DEEP INTELLIGENCE

- ✅ **schema_v66** — ALTER TABLE infra_memory_accounts ADD COLUMN avg_duration_s FLOAT DEFAULT 0
- ✅ **infra_memory duration tracking** — `record_account_op(duration_s=...)`: скользящее среднее времени выполнения per-аккаунт/op_type
- ✅ **get_account_avg_duration()** — новая функция, возвращает реальное среднее время если ≥3 samples
- ✅ **Prediction Engine learning** — `_predict_impl` использует `get_account_avg_duration()` (70% история + 30% baseline) вместо только статичных констант
- ✅ **health_score в Account Intelligence** — `_analyze_accounts_impl` запрашивает `health_score` из БД, включает в Suitability Score (trust 30% + health 15% + risk 30% + reliability 25%)
- ✅ **Proxy recommendations в PreLaunchIntelligence** — `_pre_launch_impl` запрашивает `analyze_proxies()` параллельно, добавляет `recommended_proxies` + `all_proxies` в intel
- ✅ **format_pre_launch_block proxy line** — показывает "🌐 Прокси: ✅ N пригодны · ⚠️ M плохих" если есть прокси
- ✅ **flush/load avg_duration_s** — персистируется в БД через infra_memory flush loop, загружается при рестарте

### ✅ ЗАКРЫТО (r24) — EPOCH II: MEMORY FEEDBACK LOOP + BUG HUNTER

**Замыкание Memory Feedback Loop (duration_s):**
- ✅ **op_worker join/leave** — dur_ms уже был доступен, теперь передаётся в record_account_op
- ✅ **op_worker publish** — t0_pub перед post_to_channel, pub_dur_s после успеха
- ✅ **op_worker global_presence_channel** — t0_gp перед create_channel
- ✅ **op_worker global_presence_bot** — t0_gp_bot перед create_bot_via_botfather
- ✅ **op_worker top-level success** — использует уже вычисленный duration_seconds
- ✅ **parser.py** — import time, t0_parse для обоих parse_members + parse_active_users
- ✅ **dm_engine.py** — import time, t0_dm перед send_dm, duration_s на "sent"
- ✅ **strike_engine.py** — t0_strike перед report_peer_deep_v2, duration_s на успехе
- ✅ **account_warmer.py** — import time, t0_action на каждое warmup-действие

**BUG HUNTER MODE — найдено и исправлено 4 бага:**
- ✅ **КРИТИЧЕСКИЙ: flush_to_db double-counting** — `successes + EXCLUDED.successes` → `EXCLUDED.successes`
  (каждый flush после первого удваивал исторические счётчики; то же для proxies)
- ✅ **format_pre_launch_block: двойной счёт кулдаун-аккаунтов** — cooling ⊆ excluded
  → разделено на `cooling` + `excluded_other` (непересекающиеся множества)
- ✅ **_pre_launch_impl: мёртвая ветка** — `elif pressure_score >= 85` недостижима
  (уже поймана выше через `risk.safe_to_proceed=False`), удалена
- ✅ **_predict_impl: avg_memory_score неточный знаменатель** — делил на `account_count`
  вместо `len(ranked_slice)`, занижало среднее при малом числе аккаунтов

*Последнее обновление: 2026-06-03 (r24)* *Следующий build-номер: r25*

### ✅ ЗАКРЫТО (r25) — EPOCH III: ECOSYSTEM INTEGRATION

**Global Presence → Ecosystem auto-creation:**
- ✅ **global_presence.py** — при запуске плана автоматически создаётся экосистема с типом `global_presence`
- ✅ **schema_v68.sql** — `ecosystem_id` добавлен в `global_presence_plans`
- ✅ **op_worker _exec_global_presence_channel** — созданные каналы и аккаунты добавляются в экосистему
- ✅ **op_worker _exec_global_presence_bot** — созданные боты и аккаунты добавляются в экосистему

**Strike + Mass Ops — Ecosystem контекст:**
- ✅ **strike.py confirm screen** — показывает состояние активных экосистем (health %) перед запуском
- ✅ **mass_ops.py _intel_block** — добавлен блок с состоянием экосистем к intelligence-блоку

**Enhanced Drift Detection (ecosystem_brain.py):**
- ✅ Правило 4: заблокированные аккаунты (is_banned) в экосистеме
- ✅ Правило 5: нет активности 7+ дней (застой)
- ✅ Правило 6: высокий cooldown ratio (>= 60%)
- ✅ Правило 7: нет каналов/групп/ботов в экосистеме с аккаунтами

*Последнее обновление: 2026-06-03 (r25)* *Следующий build-номер: r26*

