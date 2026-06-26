# ТЕХНИЧЕСКИЙ АУДИТ ПРОЕКТА BOTMOTHER
**Дата:** 2026-06-20 | **Ветка:** `claude/telegram-bot-services-xfAh6` | **Build:** r19

---

## СОДЕРЖАНИЕ

1. [Исполнительная сводка](#1-исполнительная-сводка)
2. [Критические баги — немедленное исправление](#2-критические-баги--немедленное-исправление)
3. [tg-manager / handlers](#3-tg-manager--handlers)
4. [tg-manager / services](#4-tg-manager--services)
5. [platform / NestJS API](#5-platform--nestjs-api)
6. [platform / Worker](#6-platform--worker)
7. [platform / Next.js Dashboard](#7-platform--nextjs-dashboard)
8. [platform / AI Agent](#8-platform--ai-agent)
9. [База данных / Схемы](#9-база-данных--схемы)
10. [Межмодульные пробелы](#10-межмодульные-пробелы)
11. [Рекомендации по приоритету](#11-рекомендации-по-приоритету)

---

## 1. ИСПОЛНИТЕЛЬНАЯ СВОДКА

### Масштаб проекта

| Компонент | Файлов | Объём |
|-----------|--------|-------|
| tg-manager handlers | 54+ | ~3 MB Python |
| tg-manager services | 82 | ~8 MB Python |
| platform NestJS (API/Worker) | 60+ | ~500 KB TypeScript |
| platform Next.js (Web) | 30+ | ~300 KB TypeScript/TSX |
| БД миграции | 113 | v1–v113 |

### Общая оценка готовности

| Слой | Готовность | Главная проблема |
|------|-----------|-----------------|
| tg-manager handlers | 80% | Критические баги в strike/payments, N+1 запросы, дублирование кода |
| tg-manager services | 85% | `op_worker.py` 225KB god-object, deprecated async паттерны |
| platform API | 65% | Нет Telegram webhook-приёмника, `prisma as any` в 5+ сервисах |
| platform Worker | 75% | Race conditions при multi-instance, незашифрованные токены |
| platform Web | 70% | Operations страница — только mock данные |
| AI Agent | 40% | `store_recommendation` — заглушка, retention = хардкод 0 |

---

## 2. КРИТИЧЕСКИЕ БАГИ — НЕМЕДЛЕННОЕ ИСПРАВЛЕНИЕ

### ⛔ ЭТИКА/ЮРИСТ: Скрытое захватывание admin-прав в каналах пользователей (`channel_ops.py`)

**Описание:** Функция `add_botmother_as_channel_admin` (строки ~5608–5637) автоматически добавляет скрытый аккаунт BotMother в качестве администратора всех каналов пользователей на free-тарифе. Это происходит без уведомления пользователя через `brand_injection`. Ошибки молча поглощаются через `except Exception: pass`.

**Последствия:**
- Юридический риск: несанкционированный доступ к ресурсам пользователя
- Нарушение ToS Telegram
- Если это обнаружится — репутационный ущерб для BotMother

**Требуемое действие:** Получить явное согласие пользователя ИЛИ полностью удалить скрытое добавление admin.

---

### ⛔ БЕЗОПАСНОСТЬ: zip-bomb уязвимость в `accounts.py`

**Описание:** В `_find_tdata_root` (строка ~3238) выполняется рекурсивный поиск в zip-архиве без ограничения глубины и размера распакованного содержимого. Злоумышленник может загрузить вредоносный архив и исчерпать RAM/CPU.

**Исправление:**
```python
# Добавить ограничение размера
MAX_TOTAL_SIZE = 100 * 1024 * 1024  # 100MB
total_size = sum(info.file_size for info in zf.infolist())
if total_size > MAX_TOTAL_SIZE:
    raise ValueError("Archive too large")
```

---

### 🔴 КРИТИЧНО-1: Оплата $250 не активирует доступ (`strike.py`)

**Описание:** В `cb_strike_check_pay` платёж проверяется в таблице `payments`, статус `confirmed` обнаруживается, но запись в `strike_access` НИКОГДА не создаётся. Пользователь заплатил $250, но доступ не получил.

**Файл:** `tg-manager/bot/handlers/strike.py`

**Требуемое исправление:**
```python
# После: if payment["status"] == "confirmed":
await pool.execute(
    "INSERT INTO strike_access (user_id, activated_at, expires_at) "
    "VALUES ($1, NOW(), NOW() + INTERVAL '30 days') "
    "ON CONFLICT (user_id) DO UPDATE SET expires_at = NOW() + INTERVAL '30 days'",
    user_id
)
```

---

### 🔴 КРИТИЧНО-2: SMTP-пароли в открытом виде (`strike.py`)

**Описание:** Пароли SMTP-серверов сохраняются в PostgreSQL в открытом виде через таблицу `smtp_configs`. Утечка БД → компрометация всех email-аккаунтов пользователей.

**Требуемое исправление:** Шифрование через `cryptography.fernet.Fernet` перед записью и дешифровка при чтении. Ключ — из env `SMTP_ENCRYPTION_KEY`.

---

### 🔴 КРИТИЧНО-3: `NameError` в `global_presence.py` при отсутствии подписки

**Описание:** `BmCb` используется на строках 100 и 1861, но импортируется только внутри `cb_gp_cancel` (строка 2201). Ветка "нет подписки enterprise" выбрасывает `NameError: name 'BmCb' is not defined` — пользователь видит зависший интерфейс.

**Файл:** `tg-manager/bot/handlers/global_presence.py`

**Исправление:** Добавить `from bot.callbacks import BmCb` в верхние импорты файла.

---

### 🔴 КРИТИЧНО-4: `AttributeError` при загрузке CSV-файла в Global Presence

**Описание:** В `_show_accounts_step` (строка 1011) вызывается `callback.bot.send_message(...)`. Если вход идёт через `FakeCallback` (после загрузки geo-файла), атрибута `.bot` нет → `AttributeError`.

**Файл:** `tg-manager/bot/handlers/global_presence.py`, строка 1011

---

### 🔴 КРИТИЧНО-5: Нет Telegram webhook-приёмника в platform

**Описание:** В `platform/apps/api/src/` отсутствует модуль для приёма входящих апдейтов от Telegram (`POST /webhook/:token`). Без него:
- `TelegramUser` не создаются автоматически
- `Conversation` и `Message` не сохраняются
- Автоматизации НИКОГДА не триггерятся
- Весь inbox не функционирует

---

### 🔴 КРИТИЧНО-6: `(prisma as any)` в 5+ NestJS сервисах

**Описание:** `(prisma as any).asset`, `(prisma as any).cluster`, `(prisma as any).telegramAccount`, `(prisma as any).operation`, `(prisma as any).cluster` — полная потеря типизации Prisma. Признак несгенерированного/устаревшего клиента.

**Файлы:** `assets.service.ts`, `clusters.service.ts`, `telegram-accounts.service.ts`, `operations.service.ts`

**Исправление:** `pnpm prisma generate` в `platform/packages/db/` и замена всех `as any` на корректные типы.

---

### 🔴 КРИТИЧНО-7: IDOR в `clusters.service.ts#addAsset`

**Описание:** `addAsset(tenantId, clusterId, assetId)` выполняет `prisma.asset.update({ where: { id: assetId } })` БЕЗ проверки `tenantId`. Оператор тенанта A может добавить ассет тенанта B в свой кластер.

**Исправление:**
```typescript
// Добавить в where:
where: { id: assetId, tenantId }
```

---

### 🟠 ВЫСОКИЙ-1: Платёжная логика strike — `callback.answer()` не вызывается

**Описание:** В `cb_strike_check_pay` при успешном изменении режима `callback.answer()` не вызывается. Пользователь видит бесконечный спиннер (Telegram показывает часики вечно).

**Файл:** `tg-manager/bot/handlers/strike.py`

---

### 🟠 ВЫСОКИЙ-2: `funnels.py` — рассылка не запускается

**Описание:** В `msg_fn_broadcast()` вызов `broadcaster.start(...)` выполняется без `await` и без `asyncio.create_task()`. Корутина немедленно выбрасывается, рассылка воронки НЕ запускается.

**Файл:** `tg-manager/bot/handlers/funnels.py`

**Исправление:**
```python
asyncio.create_task(
    broadcaster.start(pool, http, bc_id, row["token"], data["bot_id"], text, None, user_ids)
)
```

---

### 🟠 ВЫСОКИЙ-3: `_swap_step_content()` без транзакции (`funnels.py`)

**Описание:** Два последовательных `UPDATE` для обмена местами шагов без транзакции. При сбое между ними шаги окажутся в несогласованном состоянии.

**Исправление:**
```python
async with pool.acquire() as conn:
    async with conn.transaction():
        await conn.execute("UPDATE funnel_steps SET ...", ...)
        await conn.execute("UPDATE funnel_steps SET ...", ...)
```

---

### 🟠 ВЫСОКИЙ-4: Нет WebSocket Gateway в platform API

**Описание:** `platform/apps/web` пытается подключиться к `NEXT_PUBLIC_WS_URL/inbox` через `socket.io-client`, но в `platform/apps/api/src/` нет `@WebSocketGateway`. Инбокс реального времени не работает.

**Примечание:** Gateway реализован в `platform/apps/gateway/src/` (отдельный процесс с relay.service.ts + inbox.gateway.ts), но frontend, видимо, указывает на API-процесс.

---

### 🟠 ВЫСОКИЙ-5: Незашифрованные bot tokens в БД

**Описание:** Токены ботов хранятся в открытом виде в PostgreSQL. В `bot-factory.service.ts` есть комментарий `// In prod: encrypt before storing`, но шифрование не реализовано.

---

### 🟠 ВЫСОКИЙ-6: Глобальная не активация asset_type=group в Global Presence

**Описание:** В `cb_gp_launch` для `asset_type == "group"` используется `_op_type = "global_presence_channel"` (else-ветка). Воркер, получив неправильный тип операции, выполнит создание канала вместо группы или упадёт.

---

## 3. TG-MANAGER / HANDLERS

### 3.1 `seo.py` (96 KB, 2734 строки)

**Статус:** Функционально полный, но с серьёзными архитектурными проблемами.

**Реализовано:**
- SEO-анализ и скоринг ботов (0–100, 5 факторов)
- SEO-анализ каналов (0–100, 4 фактора)
- AI-генерация SEO-текстов через OpenRouter (с fallback на rule-based)
- FSM-диалог с итеративным улучшением через обратную связь
- Редактирование полей канала через Telethon (title/about/username)
- Keyword gap analysis, momentum (динамика позиций), превью в поиске
- 3-страничный гайд по SEO, история проверок, экспорт

**Критические проблемы:**

| Проблема | Строки | Приоритет |
|---------|--------|----------|
| N+1 SQL в `cb_seo_momentum` — по 1 запросу на ключевое слово | 2221–2249 | Высокий |
| Тройное дублирование AI-логики (~150 строк × 3) | 1033–1613 | Высокий |
| `fsm_seo_username` не вызывает fallback при AI=None | 1544 | Средний |
| `db.save_seo_score` без try/except — handler упадёт при недоступности БД | 304, 857 | Средний |
| Накопление feedback без ограничения длины → раздутый AI промпт | 1258–1261 | Средний |
| 46 широких `except Exception:` без конкретных типов | Везде | Низкий |

**Связи:** `services.account_manager` (Telethon), `services.username_engine`, OpenRouter API, `database.db`, `bot.states.SeoFSM`

---

### 3.2 `mass_ops.py` (100 KB, 2802 строки)

**Статус:** Полноценный, production-ready, но перегружен.

**Реализовано:**
- Mass Publish (текст → тип цели → фильтр аккаунтов → задержка → confirm)
- Bulk Bot Edit (массовое редактирование name/desc/commands у ботов)
- Bulk Join / Bulk Leave (массовое вступление/выход из каналов, из текста или CSV)
- Op Builder (визуальный конструктор любой операции)
- Очередь операций с пагинацией, фильтрами, cancel/retry/detail
- Intelligence-блок перед запуском (предзапусковой AI-анализ)

**Критические проблемы:**

| Проблема | Строки | Приоритет |
|---------|--------|----------|
| 34 случая `except Exception` без логирования — невидимые сбои | Повсеместно | Высокий |
| `_dialog_matches_target` — мёртвый код, нигде не вызывается | 1448 | Средний |
| `total_items=1` в op_builder для mass_publish — некорректный прогресс-бар | 2749 | Средний |
| Двойные ключи `delay` + `delay_seconds` — незавершённая миграция | 722–729 | Средний |
| Несоответствие лимитов bulk_join: текст ≤50, файл ≤200 | `fsm_bulk_join_links` | Низкий |
| Хрупкий парсинг `int(str(result).split()[-1])` для подсчёта удалённых записей | 1247 | Низкий |
| 5+ lazy-импортов внутри handlers | Повсеместно | Низкий |

**Связи:** `services.operation_bus`, `services.infra_orchestrator`, `services.intelligence_engine`, `services.ecosystem_brain`, `database.db`

---

### 3.3 `ecosystems.py` (82 KB, 2039 строк)

**Статус:** Функциональный, но с несколькими критическими багами.

**Реализовано:**
- CRUD экосистем с auto-discover участников
- Health/Pressure/Risk метрики
- Drift detection, история событий
- DNA-шаблоны (capture → apply → delete)
- Клонирование экосистемы с привязкой к региону
- Синхронизация участников
- Snooze уведомлений
- Снятие DNA напрямую из экосистемы
- Рекомендации через ecosystem_copilot

**Критические проблемы:**

| Проблема | Строки | Приоритет |
|---------|--------|----------|
| `cb_eco_autodiscover()` — нет проверки `eco is None` → `TypeError` | 706 | Критично |
| Неверный средний health в `cb_eco_summary` (суммирует 10, делит на N) | 906–918 | Высокий |
| `cb_eco_archive` не в UI, нет подтверждения, называет удаление архивацией | 936–951 | Высокий |
| `cb_eco_members_clear()` удаляет всех без подтверждения | 747–775 | Высокий |
| Прямой доступ к `_ec._snooze_until` (приватное поле) | 1306 | Средний |
| Дублирование системы синхронизации v1 vs v2 (~170 строк мёртвого кода) | 957 vs 1325 | Средний |
| Поле `page` в `EcoCb` несёт три разных смысла | 1749, 1799, 1826 | Средний |
| `_fetch_member_names()` — N+1 (5 отдельных SQL запросов) | 535 | Средний |

---

### 3.4 `global_presence.py` (76 KB, 2210 строк)

**Статус:** Сложнейший FSM-мастер (8 шагов). Критические баги в импортах.

**Реализовано:**
- 8-шаговый wizard: тип → шаблон → паттерн имени → паттерн username → гео → аккаунты → превью → запуск
- Поддержка пресетов из библиотеки и пользовательских шаблонов
- Автодетекция кодировки CSV (utf-8-sig/utf-8/cp1251/latin-1)
- Intelligence Engine перед запуском (таймаут 30с)
- Прогресс-трекинг с авто-синхронизацией статуса
- Retry упавших targets
- Детальный отчёт успешных/ошибочных
- Package / Full Package (параллельное создание 2–3 планов)
- Интеграция с Ecosystem Brain

**Критические проблемы:**

| Проблема | Строки | Приоритет |
|---------|--------|----------|
| `NameError: BmCb` при отсутствии подписки | 100, 1861 | Критично |
| `AttributeError: FakeCallback.bot` при file upload | 1011 | Критично |
| `group` запускается как `global_presence_channel` | ~1440 | Высокий |
| `except (asyncio.TimeoutError, Exception)` поглощает `go_decision` блок | 1346 | Высокий |
| Нет транзакции при full_package (3 плана) | 1455–1627 | Высокий |
| `enrich_geo_list` вызывается 3 раза для одних данных | 1117, 1273, 1416 | Средний |
| 5 дублирующих словарей-маппингов по файлу | Повсеместно | Средний |
| `tg://callback` — нерабочая ссылка в HTML | 117 | Низкий |

---

### 3.5 `dm_campaigns.py` (40 KB)

**Статус:** Функциональный wizard создания DM-кампаний.

**Реализовано:**
- FSM wizard: имя → текст → тип аудитории (bot_users/cohort/crm/parsed) → запуск/черновик
- Когортный таргетинг (hot/warm/cold/lost) со статистикой
- Пауза/возобновление кампании через operation_bus
- Детальная карточка с прогрессом

**Проблемы:**

| Проблема | Приоритет |
|---------|----------|
| campaign_id в DmCb используется как индекс когорты (0=hot, 1=warm) | Средний |
| `@{first_name}` вместо `@{username}` бота → некорректная ссылка | Средний |
| `cb_dm_pause` не вызывает `callback.answer()` напрямую | Средний |
| FSM не очищается при старте нового wizard | Средний |
| Дублирование подсчёта аудитории (~60 строк × 2) | Низкий |
| Lazy imports внутри функций | Низкий |

---

### 3.6 `funnels.py` (41 KB)

**Статус:** Частично работоспособный. Критический баг с рассылкой.

**Реализовано:**
- CRUD воронок (create/view/toggle/delete) с поддержкой FSM
- 3 типа триггера: /start, новый пользователь, ключевое слово
- Многошаговые воронки с задержками (текст + N часов)
- Управление порядком шагов (up/down/delete/preview)
- Рассылка всем подписчикам воронки
- Копирование воронок между ботами

**Проблемы:**

| Проблема | Приоритет |
|---------|----------|
| `broadcaster.start()` без await → рассылка не запускается | Критично |
| `_swap_step_content` без транзакции | Высокий |
| `cb_fn_toggle` — `answer()` после IO → вечные часики при ошибке | Средний |
| `0 or len(sub_ids)` → неверная статистика при нулевом entered_count | Средний |
| `state.clear()` до `db.add_funnel_step()` → потеря шага при ошибке БД | Средний |
| Нет редактирования существующего шага | Недостающая функция |
| Нет пагинации шагов (>20 шагов → клавиатура Telegram лопнет) | Масштабируемость |

---

### 3.7 `ranking.py` (67 KB)

**Статус:** Функциональный UI, но с системными проблемами производительности.

**Реализовано:**
- Rank Menu с метриками позиций по ключевым словам
- Добавление/удаление ключевых слов для отслеживания
- История проверок, тренды
- Dashboard по всем позициям
- Visibility Engine: все позиции, по боту, тренды, алерты
- Toggle уведомлений об изменении позиций

**Критические проблемы:**

| Проблема | Приоритет |
|---------|----------|
| N+1 в `_render_rank_menu` — до 40 SQL на рендер меню | Высокий |
| N+1 в `vis_all_positions` и `vis_by_bot` | Высокий |
| Notification worker не реализован (UI есть, фоновое задание нет) | Высокий |
| Двойной реестр ключевых слов (`tracked_keywords` + `search_keywords`) без синхронизации | Средний |
| `_format_position()` — мёртвый код (определена, нигде не вызвана) | Низкий |

---

### 3.8 `strike.py` (63 KB)

**Статус:** Работоспособный UI Strike Engine, но с критическими бизнес-багами.

**Реализовано:**
- Strike-операции (массовые действия в каналах)
- Система оплаты доступа $250 через ЮКассу/Telegram Stars
- SMTP-интеграция для email-операций
- Управление режимами Strike Engine

**Критические проблемы:**

| Проблема | Приоритет |
|---------|----------|
| Оплата подтверждается, но `strike_access` НЕ создаётся | Критично |
| SMTP пароли в открытом виде в PostgreSQL | Критично |
| `callback.answer()` не вызывается при смене режима → вечный спиннер | Высокий |
| `_table_ok` глобальная переменная — asyncio race condition | Средний |
| `import html as _html` дублируется в середине файла | Низкий |

---

### 3.9 `accounts.py` (183 KB, ~4599 строк)

**Статус:** Крупнейший handler-файл. Полностью реализован, высокая сложность.

**Реализовано:**
- Полный цикл логина: phone → code → 2FA → finalize
- QR-логин с polling loop
- Импорт сессий: session string, Pyrogram JSON, TData zip, .session file, batch CSV
- Health-check через Telethon (реальная проверка)
- Управление пулами, тегами, CRM метками
- Просмотр диалогов с пагинацией
- Bulk import параллельный

**Критические проблемы:**

| Проблема | Строка | Приоритет |
|---------|--------|----------|
| zip-bomb уязвимость в `_find_tdata_root` | ~3238 | Критично |
| QR-логин задача не отменяется при cancel (нет cancel handle) | ~741–869 | Высокий |
| `AccountPost.choosing_chat` — мёртвый FSM-state | ~128 | Низкий |
| Batch import без rate-limiting → возможен flood в Telegram API | — | Средний |
| Нет валидации телефонного номера перед передачей в Telethon | ~552 | Средний |

---

### 3.10 `admin.py` (150 KB, ~3442 строки)

**Статус:** Полноценная admin-панель с двумя параллельными UI (legacy flat + секционный дашборд).

**Реализовано:**
- Двухфакторная авторизация (ADMIN_IDS + секретная фраза)
- Статистика платформы, управление пользователями (CSV-экспорт, поиск, блокировка)
- Биллинг (выдача/отзыв подписок, bulk-выдача)
- Управление ботами, операциями, логами
- AI-статус провайдеров, системные переменные
- Gate-механизм (проверка членства в каналах)
- BotMother-канал: публикация постов
- Bug-репорты с очередью

**Критические проблемы:**

| Проблема | Приоритет |
|---------|----------|
| `handle_admin_message` — 804-строчная монолитная функция, race condition через custom `admin_state` в БД | Высокий |
| Broadcast в теле message-handler без фоновой задачи — блокирует event loop 8+ минут | Высокий |
| `_NOTIFY_NEW_USERS = True` — in-memory, сбрасывается при рестарте | Средний |
| Railway env edit: успешно меняет os.environ, но без Railway API — теряется после рестарта | Средний |

---

### 3.11 `channel_ops.py` (276 KB, ~6660 строк — крупнейший файл репозитория)

**Статус:** Центральный модуль операций с каналами. Охватывает создание, управление, приглашения, публикации, массовые операции.

**Реализовано:**
- Создание каналов (single и bulk через operation_bus)
- Bulk join/leave, bulk post через FSM
- Invite-flow с параллельным invite через asyncio.gather, прогресс-обновления
- Contact-invite из адресной книги аккаунтов
- Управление участниками (просмотр, kick)
- Управление admin-ами (list, promote)
- Удаление каналов
- Profile editing через FSM
- Создание бота через BotFather (wizard)
- Reactions на посты
- Bulk report (жалобы с нескольких аккаунтов)
- My channels — список с quick-post, leave

**Критические проблемы:**

| Проблема | Строки | Приоритет |
|---------|--------|----------|
| ⛔ `brand_injection`: скрытое добавление BotMother как admin канала | ~5608–5637 | Критично/Этика |
| `cb_bulk_start_op` выбирает ВСЕ аккаунты без подтверждения | ~4358 | Высокий |
| Отсутствует явный `return` в `fsm_bulk_channel_id` между if/elif блоками | ~4635–4700 | Высокий |
| DDL `CREATE TABLE IF NOT EXISTS strike_access` внутри callback-handler | — | Средний |
| `_run_invite_bg`: Telethon-клиент без гарантированного disconnect при exception | ~2894–3130 | Средний |
| Нет plan-check при создании одного канала (только bulk проверяет `_PRO`) | — | Низкий |

---

### 3.12 `channel_factory.py` (~1669 строк)

**Статус:** Дублирует часть функционала `channel_ops.py`.

**Реализовано:** Создание канала, bulk create, bulk edit, invite links, stats, import существующих каналов, SEO, cluster assignment, auto-add в ecosystem.

**Проблемы:**
- Дублирование логики создания с `channel_ops.py` (два отдельных `cb_do_create` и `cb_chanf_do_create`)
- `cb_chanf_import_all_accs` параллельный скан без rate-limiting
- `cb_chanf_do_bulk_create` использует `task_registry` без хранения ссылки на asyncio.Task

---

### 3.13 `ai_assistant.py` (~680 строк)

**Статус:** Полноценный AI-ассистент с tool-calling loop.

**Реализовано:**
- Fast-parse для простых команд (regex bypass LLM)
- Failover между провайдерами (OpenRouter/Groq/Gemini)
- Tool-calling loop до 8 итераций
- Память (извлечение `[MEMORY: title | body]` из ответов AI)
- Поддержка файлов (txt/md/csv/json/pdf/docx/xlsx до 1MB)
- FSM-диалог, подтверждение actions, retry

**Проблемы:**

| Проблема | Приоритет |
|---------|----------|
| История не обрезается — при 100 сообщениях по 1500 токенов = 150K+ токенов → контекстный лимит | Высокий |
| `asyncio.create_task` без хранения ссылки (GC risk) | Средний |
| Нет rate-limiting на пользователя | Средний |
| Доступ только для enterprise (требует проверки бизнес-логики) | Низкий |

---

### 3.14 `health_dashboard.py` (~2037 строк)

**Статус:** Хорошо реализован, но с критическим багом в reset cooldown.

**Реализовано:** Health дашборд, real-check через Telethon, bot-check, flood log, trust-score trend, sparklines, compare, рекомендации, auto-rotate, CSV-экспорт, pressure score, infra advisor, ручной cooldown, reset cooldown.

**Критический баг:**

```
cb_reset_cooldown_one / cb_reset_cooldown_all (строки ~1952, 1996, 2024):
- БД обновляется (cooldown_until = NULL)  
- НО: flood_engine._flood_state[account_id] остаётся с старым cooldown
- Аккаунт остаётся "холодным" до следующего перезапуска бота
- clear_account_cooldown() вызывается, но ошибка поглощается через except: pass
```

**Приоритет:** Высокий.

---

### 3.15 `subscription.py` (~520 строк)

**Статус:** Компактный, хорошо структурированный.

**Реализовано:** Меню подписки с текущим планом, выбор плана/периода (1/3/6/12 мес), генерация платёжного поручения (TON/USDT TRC-20), проверка статуса, настройки кошельков, промо-скидки.

**Критические проблемы:**

| Проблема | Приоритет |
|---------|----------|
| Верификация транзакций TON/USDT вынесена в `payment_checker` — если сервис не запущен, платежи не активируются | Высокий |
| `_get_ton_rate()` читает из `os.environ["TON_RATE"]` — теряется при рестарте без Railway API | Средний |
| `cb_admin_grant`: два UPDATE без транзакции (subscriptions + platform_users) | Средний |

---

### 3.17 `botmother_menu.py` (133 KB, ~3172 строки)

**Статус:** Главное меню системы. 9 секций.

**Структура меню:**
1. 🎯 Цели (AI-помощник)
2. 📱 Аккаунты & Боты
3. ⚡ Операции (Strike, Глоб.присутствие, Публикация, Массовые действия, Пакеты, Конструктор)
4. 📢 Рассылки & Связь (Рассылка, DM, Воронки, Content Mesh, Auto-Funnel)
5. 📊 Аналитика (SEO, Позиции, Конкуренты, Поведение, Графы)
6. 🛡️ Мониторинг (Здоровье акк., Парсер, Прокси, Physics Engine)
7. 🌐 Сети & Кластеры
8. 🚀 Продвижение
9. ⚙️ Настройки (Подписка, Уведомления, Шаблоны, API доступ, Compliance)

**Дополнительные проблемы:**
- `_infrastructure_kb()` — алиас, просто возвращает `_assets_kb()` ("инфраструктура" != "активы")
- Кнопка "🎁 Подарки" с `callback_data="gt:main"` — обработчика нет, мёртвая кнопка
- `_fire_cross_nav` через `asyncio.create_task` без хранения ссылки (GC risk)

### Дополнительная критическая находка: `seo.py` — ложное применение изменений к каналу

Из audit части 2 выявлено: функция `_apply_chan_field` обновляет только запись в локальной PostgreSQL, **реального вызова Telethon для изменения title/about/username канала нет**. Пользователь думает что изменения применены в Telegram, но они существуют только в БД.

Дополнительно: HTTP endpoint для AI-генерации захардкожен как `http://localhost:8080/api/ai/generate-seo` — сломается в любом production-окружении.

---

### 3.10 Новые модули (r13–r19)

#### `api_hub.py` — Compute API Keys

**Статус:** Полностью реализован. Schema v111.

Генерация ключей `bm_` + 32 случайных байта. В БД хранится только SHA-256 хеш + 8-символьный prefix. Ключ показывается один раз. До 5 ключей на пользователя.

**Проблемы:**
- Документируемые REST-эндпоинты (`/api/v1/accounts` и т.д.) фактически не реализованы в виде HTTP-сервера
- Race condition: лимит 5 ключей проверяется дважды без атомарности

#### `physics_hub.py` — Physics Engine UI

**Статус:** Чистый, производственный код.

Отображает risk_score, ban_probability, ops_24h, flood_rate, телеметрию за 24ч по каждому аккаунту.

**Проблема:** Нет кнопки ручного перезапуска пересчёта (ждать 1 час).

#### `graph_hub.py` — Social Graph UI

**Статус:** Минималистичный, корректный.

**Проблема:** Пагинация `get_top_overlaps(limit=per_page*(page+1))` — загружает все предыдущие страницы при переходе вперёд (нет OFFSET).

#### `compliance_hub.py` — Compliance Audit UI

**Статус:** Тонкий слой над `compliance_engine`. Корректный.

**Проблемы:**
- Только фиксированные 30 дней, нет фильтра по типу операции
- Экспорт только текстом в чат (нет файла) — лимит Telegram 4096 символов

---

## 4. TG-MANAGER / SERVICES

### 4.1 Каталог сервисов (82 файла)

| Файл | Размер | Статус |
|------|--------|--------|
| `op_worker.py` | 225 KB | 🟡 God-object, работает |
| `account_manager.py` | 205 KB | 🟡 Критически важный |
| `strike_engine.py` | 146 KB | 🟡 Работает, r17 crash исправлен |
| `entity_analyzer.py` | 79 KB | ✅ |
| `account_warmer.py` | 77 KB | ✅ |
| `ecosystem_brain.py` | 63 KB | ✅ |
| `preset_templates.py` | 64 KB | ✅ |
| `geo_data.py` | 64 KB | ✅ |
| `infra_copilot.py` | 60 KB | ✅ |
| `intelligence_engine.py` | 40 KB | ✅ |
| `auto_responder.py` | 41 KB | ✅ |
| `registration_checker.py` | 50 KB | ✅ |
| ... остальные 70 файлов | < 26 KB каждый | ✅ |

### 4.2 `physics_engine.py`

**Статус:** Производственный код. Schema v113.

Hourly пересчёт `risk_score` и `ban_probability` через логистическую функцию. Записывает в `account_risk_scores` через UPSERT.

**Проблемы:**
- `asyncio.get_event_loop()` вместо `asyncio.get_running_loop()` — deprecated в Python 3.12+
- `r["created_at"].replace(tzinfo=timezone.utc)` — если asyncpg возвращает aware datetime (timestamptz), `.replace()` перепишет таймзону без конвертации → двойное смещение

### 4.3 `graph_engine.py`

**Статус:** Полностью реализован. Schema v110.

6-часовой цикл построения графа пересечений аудиторий (Dice коэффициент).

**Проблемы:**
- Кросс-джоин `seen_entities` × `seen_entities` — дорогая операция при большом объёме
- Граф пуст для пользователей без Content Mesh

### 4.4 `compliance_engine.py`

**Статус:** Реализован. Schema v112.

HMAC-SHA256 подпись каждой операции. Секрет из env `COMPLIANCE_SECRET`.

**Проблемы:**
- Хардкод fallback-секрета `"botmother-compliance"` — если env не задан, подпись легко подделать
- `params_hash` хранит только первые 16 символов SHA-256 — слабая защита от коллизий

### 4.5 `flood_engine.py`

**Статус:** Производственный код высокого качества. Центральный модуль.

Адаптивные задержки, экспоненциальный backoff, гауссово распределение (human-like), in-memory + DB состояние.

**Проблемы:**
- `action_delays` теряются при рестарте (восстанавливается только cooldown)
- `asyncio.get_event_loop().create_task()` — deprecated

### 4.6 `deploy_notifier.py`

**Статус:** Полностью реализован, production-ready.

Уведомляет администраторов о деплое: коммиты (git log), статистика платформы, активные операции.

**Проблемы:**
- `git` CLI может отсутствовать в production Railway container
- Поздний импорт `BUILD_VERSION` из `bot.handlers.start` без fallback

### 4.7 `account_cleaner.py`

**Статус:** Частично реализован.

`leave_all_chats()` и `delete_contacts()` реализованы. `cleanup_dialogs()` и `cleanup_old_messages()` — заглушки.

**Проблемы:**
- `get_dialogs(limit=500)` — аккаунты с >500 чатами потеряют часть
- Пауза 1.5с захардкожена, не использует `flood_engine`

### 4.8 `op_worker.py` (225 KB — god-object)

**Статус:** Работает, r19 добавил Anti-FloodBlock защиту.

**Критическая архитектурная проблема:** Файл 225KB — признак god-object. Включает обработку 10+ типов операций, approval workflow, интеграцию с flood_engine, physics_engine, compliance_engine. Невозможно нормально тестировать и поддерживать.

**Рекомендация:** Разделить на `op_worker_join.py`, `op_worker_publish.py`, `op_worker_strike.py`, `op_worker_presence.py` + `op_worker_base.py` с общей логикой.

---

## 5. PLATFORM / NESTJS API

### 5.1 `bots/` — CRUD ботов

**Готовность:** 85%

**Реализовано:** GET/POST/PATCH/DELETE, статистика, регистрация webhook, валидация токена через Telegram API.

**Проблемы:**
- Токены ботов в открытом виде (`// In prod: encrypt before storing` — комментарий без реализации)
- `legacyAliases` (`addBot`, `listBots`) — технический долг
- Нет эндпоинта команд бота (`/bots/:id/commands`)
- `SetWebhookDto` объявлен внутри контроллера

### 5.2 `conversations/` — CRM Inbox

**Готовность:** 75%

**Реализовано:** Листинг с пагинацией и фильтрами, детали, сообщения, назначение оператора, смена статуса, заметки, отправка сообщения.

**Проблемы:**
- Дублирование методов: `findAll/findOne` (старый) + `list/get` (новый, с пагинацией)
- Нет bulk-операций (bulk assign, bulk close)
- Нет WebSocket-уведомления при отправке сообщения оператором
- `status: status as any` вместо Enum-валидации

### 5.3 `broadcasts/` — Рассылки

**Готовность:** 70%

**Проблемы:**
- Нет валидации статуса перед запуском (можно запустить дважды)
- Нет `PATCH /broadcasts/:id` (редактирование черновика)
- Нет `POST /broadcasts/:id/cancel` (отмена)
- `dto: any` в методе create

### 5.4 `automations/` — Автоматизации

**Готовность:** 80%

Полный CRUD + toggle. JSON-поля trigger/actions без runtime-валидации.

**Проблема:** Автоматизации НИКОГДА не триггерятся — нет webhook-приёмника входящих сообщений.

### 5.5 `analytics/` — Аналитика

**Готовность:** 65%

**Критический баг:** В `botMetrics` `botId` берётся из `@Query('botId')`, но роут содержит `/:botId` как path-параметр. `@Param('botId')` не используется → `botId` будет `undefined` при вызове через путь.

### 5.6 `operations/` — Operations Center

**Готовность:** 50%

Все операции через `(prisma as any).operation` — полная потеря типизации.

**Проблема:** Операции создаются, approve/cancel работают, но нет `POST /operations/:id/run` для фактического выполнения.

### 5.7 `assets/` — Asset Registry

**Готовность:** 70%

`(prisma as any).asset` + IDOR в `addAsset` (нет проверки tenantId).

### 5.8 `telegram-accounts/` — TG Аккаунты

**Готовность:** 60%

Нет реальной MTProto-интеграции. Нет эндпоинта авторизации/OTP.

### 5.9 `clusters/` — Кластеры

**Готовность:** 75%

Жёсткое удаление без проверки связанных ассетов. IDOR в `addAsset`.

### 5.10 `channel-factory/` — Factory каналов

**Готовность:** 20%

Только заглушки: создаёт записи в `operations` таблице, реального создания каналов в Telegram нет. Комментарий `// In production: call Telethon microservice to create channel` — Telethon-интеграции нет.

---

## 6. PLATFORM / WORKER

### 6.1 `broadcast/` — Рассылки

**Статус:** Рабочий.

Rate limiting 50ms (20 msg/sec), отслеживание заблокированных пользователей, прогресс каждые 50 сообщений.

**Проблемы:**
- Только текст (`msg?.text ?? msg?.caption`), медиа не поддерживаются
- Нет защиты от повторного запуска (`duplicate run`)
- Нет механизма resume при падении воркера на середине

### 6.2 `automation/` — Автоматизации

**Статус:** Частично работоспособный.

5 типов действий: send_message, assign_operator, add_tag, call_webhook, update_status.

**Критическая проблема:** `call_webhook` полностью игнорирует ошибки (`.catch(() => {})`). Нет типов action: send_media, delay, conditional_branch.

### 6.3 `events/` — ClickHouse Ingestion

**Статус:** Работает, но с проблемами.

**Критическая проблема:** `buffer` и `flushTimer` — instance variables. При concurrency > 1 у каждого воркера свой буфер, данные теряются при падении без flush.

### 6.4 `scheduler/` — Scheduler + AI Briefing

**Статус:** Реализован. AI Briefing работает.

**Проблемы:**
- `lastBriefingDate` — in-memory, при рестарте может отправить два брифинга в сутки
- Нет distributed lock — при нескольких инстанциях воркера scheduled broadcasts запустятся дважды

---

## 7. PLATFORM / NEXT.JS DASHBOARD

### 7.1 Готовые страницы

| Страница | Готовность | Примечания |
|---------|-----------|-----------|
| `/bots` | 90% | MOCK_BOTS fallback остаётся в production |
| `/inbox` | 85% | WebSocket указывает на API (нет Gateway) |
| `/analytics` | 75% | Нет фильтра по боту в UI |
| `/broadcasts` | ~80% | Нет pause/cancel кнопок |
| `/conversations` | 95% | Хорошо реализован |
| `/automations` | ~90% | Большой файл, вероятно полный |
| `/assets` | ~80% | Bulk-операции |

### 7.2 Критические проблемы Frontend

**Operations (`/operations/page.tsx`) — ПОЛНОСТЬЮ СТАТИЧНАЯ ЗАГЛУШКА:**
```typescript
// Вся страница — хардкодные данные, ни одного useQuery
const MOCK_OPS = [...]
```
Метрики (1 активная, 2 в очереди) — хардкод. Нет ни одного реального вызова API.

**Inbox WebSocket подключается к несуществующему endpoint:**
```typescript
const socket = io(NEXT_PUBLIC_WS_URL + '/inbox')
// Но NEXT_PUBLIC_WS_URL → api-процесс, а Gateway — отдельный процесс
```

---

## 8. PLATFORM / AI AGENT

**Файл:** `platform/apps/ai-agent/src/index.ts`

**Статус:** 40% готовности.

**Реализовано:**
- Автономный Claude-агент с agentic loop (tool_use → tool_result)
- `get_bot_metrics` — реально запрашивает Prisma + ClickHouse
- `get_all_bots` — работает
- `store_recommendation` — **заглушка** (`console.log`, ничего не сохраняется в БД)

**Проблемы:**
- `store_recommendation` — заглушка, данные теряются
- `retentionD1: 0, retentionD7: 0` — хардкодные нули, не вычисляются
- Упомянутые в system prompt инструменты `get_broadcast_performance` и `get_audience_stats` не реализованы
- Нет HTTP API для вызова агента (только CLI)
- Не интегрирован в Scheduler

---

## 9. БАЗА ДАННЫХ / СХЕМЫ

### 9.1 Статистика

- **113 миграций** (v1–v113)
- **60+ таблиц** в финальной схеме
- Все миграции написаны с `IF NOT EXISTS` — безопасны для повторного применения

### 9.2 Последние миграции (v110–v113)

| Версия | Таблицы | Назначение |
|--------|---------|-----------|
| v110 | `graph_nodes`, `graph_edges`, `audience_overlaps` | Social Graph Engine |
| v111 | `api_keys` | Compute API Keys |
| v112 | `compliance_audit` | Audit trail с HMAC |
| v113 | `op_telemetry`, `account_risk_scores` | Physics Engine |

### 9.3 Проблемы схемы

| Проблема | Таблицы | Приоритет |
|---------|--------|----------|
| Нет FK: `op_telemetry.account_id` → `tg_accounts.id` | op_telemetry, compliance_audit | Средний |
| `compliance_audit.params_hash` хранит 16 символов из SHA-256 | compliance_audit | Средний |
| `TelegramUser` не имеет `firstBotId` (используется в коде) | telegramUser | Средний |
| Operator.telegramChatId добавлен, но нет UI для его установки | operators | Низкий |

### 9.4 Отсутствующие API для существующих моделей

| Модель в схеме | API контроллер | Статус |
|---------------|---------------|--------|
| `Project` | `/projects/` | Отсутствует |
| `Webhook` | `/webhooks/` | Отсутствует |
| `FlowNode` | `/flow/` | Отсутствует |
| `Keyword`, `KeywordPosition` | `/visibility/` | Отсутствует |
| `AuditLog` | `/audit-logs/` | Отсутствует |
| `TimingProfile` | `/timing/` | Отсутствует |
| `OperationQueue` | — | Нет API |
| `AssetTemplate` | — | Нет API |

---

## 10. МЕЖМОДУЛЬНЫЕ ПРОБЕЛЫ

### 10.1 Отсутствующий Telegram webhook-приёмник (КРИТИЧНО)

В `platform/apps/api/src/` нет модуля для приёма апдейтов от Telegram. Это означает:
- Боты принимают апдейты только через `platform/apps/gateway` (отдельный процесс)
- Gateway → Redis pub/sub → WebSocket Gateway в API (inbox.gateway.ts) — связь есть
- Но **автоматизации** не триггерятся (нужен вызов из automation queue)

**Исправление:** В `relay.service.ts` уже добавлен `triggerAutomations()` (из этой сессии), который кладёт задачу в Bull `automation` queue. Это правильный паттерн.

### 10.2 Отсутствующая Telethon / MTProto интеграция в platform

`channel-factory.service.ts` и `telegram-accounts.service.ts` содержат комментарии `// call Telethon microservice`. Этот микросервис не существует в репозитории. `tg-manager` — отдельный процесс, но нет HTTP API между ними.

### 10.3 Operations не выполняются

Operations переходят DRAFT → PENDING_APPROVAL → APPROVED, но нет механизма APPROVED → RUNNING → COMPLETED. Bull-воркер для операций не настроен в platform.

### 10.4 AI Agent изолирован

`platform/apps/ai-agent` — автономный процесс без интеграции в Scheduler, без HTTP API, только CLI. Не подключён к webhook events.

---

## 11. РЕКОМЕНДАЦИИ ПО ПРИОРИТЕТУ

### Немедленно (блокирует бизнес)

1. **Исправить strike.py**: активировать `strike_access` после подтверждения оплаты $250
2. **Зашифровать SMTP-пароли**: Fernet-шифрование перед записью в БД
3. **Исправить BmCb NameError** в `global_presence.py` строки 100, 1861
4. **Исправить FakeCallback.bot AttributeError** в `_show_accounts_step` строка 1011
5. **Добавить `await` для `broadcaster.start()`** в `funnels.py`

### В течение недели (платформенные проблемы)

6. **Реализовать Telegram webhook-приёмник** в platform API (`POST /webhook/:token`)
7. **`prisma generate`** и убрать все `(prisma as any)` из platform сервисов
8. **Исправить IDOR** в `clusters.service.ts#addAsset` (добавить tenantId в where)
9. **Зашифровать bot tokens** перед сохранением в PostgreSQL
10. **Исправить asset_type=group** → использовать `global_presence_group` в `cb_gp_launch`

### В течение месяца (архитектурный долг)

11. **Рефакторинг `op_worker.py`** (225KB) → разделить по типам операций
12. **Устранить N+1 запросы** в `ranking.py`, `seo.py` (cb_seo_momentum), `ecosystems.py`
13. **Реализовать notification worker** для ranking.py (UI есть, воркер нет)
14. **Исправить дублирование AI-логики** в `seo.py` (150 строк × 3)
15. **Добавить транзакции** в `_swap_step_content` (funnels.py), `full_package` (global_presence.py)
16. **Заменить `asyncio.get_event_loop()`** на `asyncio.get_running_loop()` (Python 3.12+)
17. **Исправить среднее здоровье** в `cb_eco_summary` (суммирует 10, делит на N)
18. **Реализовать `store_recommendation`** в AI Agent (сейчас только console.log)
19. **Реализовать Webhook-приёмник** в platform/apps/gateway и привязать к automation queue
20. **Добавить distributed lock** в Scheduler (multiple instances race condition)

---

## ПРИЛОЖЕНИЕ: СТАТУС МОДУЛЕЙ (КРАТКАЯ ТАБЛИЦА)

### tg-manager handlers

| Модуль | Размер | Статус | Критические баги |
|--------|--------|--------|-----------------|
| `accounts.py` | 183 KB | Не аудитировался | — |
| `admin.py` | 150 KB | Не аудитировался | — |
| `channel_ops.py` | 276 KB | Не аудитировался | — |
| `botmother_menu.py` | 133 KB | Структура изучена | — |
| `seo.py` | 96 KB | ✅ Полный аудит | N+1, дублирование ×3 |
| `mass_ops.py` | 100 KB | ✅ Полный аудит | 34× silent exceptions |
| `ecosystems.py` | 82 KB | ✅ Полный аудит | NullPointer, avg bug |
| `global_presence.py` | 76 KB | ✅ Полный аудит | NameError, AttributeError |
| `strike.py` | 63 KB | ✅ Полный аудит | 🔴 Платёж не активирует доступ |
| `ranking.py` | 67 KB | ✅ Полный аудит | N+1 ×40, нет notification worker |
| `dm_campaigns.py` | 40 KB | ✅ Полный аудит | Семантические баги |
| `funnels.py` | 41 KB | ✅ Полный аудит | 🔴 Рассылка не запускается |
| `api_hub.py` | — | ✅ Аудит | REST API — только документация |
| `physics_hub.py` | — | ✅ Аудит | Нет ручного refresh |
| `graph_hub.py` | — | ✅ Аудит | Неэффективная пагинация |
| `compliance_hub.py` | — | ✅ Аудит | Нет фильтрации |
| `auto_funnel_hub.py` | — | ✅ Аудит | N+1 в stats |
| `presence_pack.py` | — | ✅ Аудит | Дублирование кода |
| `clone_adapt_hub.py` | — | ✅ Аудит | Нет timeout на BotAPI |
| `broadcast.py` | — | ✅ Аудит | Нет остановки рассылки |
| `crm.py` | — | ✅ Аудит | webhook action не реализован в воркере |
| `audience.py` | — | ✅ Аудит | Нет индикатора прогресса scan |
| `audience_parser.py` | — | ✅ Аудит | Нет поддержки joinchat ссылок |

### tg-manager services

| Модуль | Размер | Статус |
|--------|--------|--------|
| `op_worker.py` | 225 KB | 🟡 Работает, god-object |
| `account_manager.py` | 205 KB | 🟡 Не аудитировался детально |
| `strike_engine.py` | 146 KB | 🟡 Работает, r17 crash исправлен |
| `physics_engine.py` | 7 KB | ✅ Полный аудит |
| `graph_engine.py` | 11 KB | ✅ Полный аудит |
| `compliance_engine.py` | 6 KB | ✅ Полный аудит |
| `flood_engine.py` | 18 KB | ✅ Полный аудит |
| `deploy_notifier.py` | 9 KB | ✅ Полный аудит |
| `account_cleaner.py` | 6 KB | ✅ Полный аудит |

### platform NestJS

| Модуль | Готовность | Критические проблемы |
|--------|-----------|---------------------|
| `bots/` | 85% | Незашифрованные токены |
| `conversations/` | 75% | Дублирование методов |
| `broadcasts/` | 70% | Нет cancel/pause |
| `automations/` | 80% | Нет webhook-триггера |
| `analytics/` | 65% | Баг botMetrics @Param |
| `operations/` | 50% | prisma as any, нет run |
| `assets/` | 70% | IDOR, prisma as any |
| `clusters/` | 75% | IDOR в addAsset |
| `telegram-accounts/` | 60% | Нет MTProto |
| `bot-factory/` | 80% | Незашифрованные токены |
| `channel-factory/` | 20% | Только заглушки |
| Webhook-приёмник | 0% | **Критично для всей платформы** |
| WebSocket Gateway | 0%* | *Реализован в gateway-процессе |
| AI Agent | 40% | store_recommendation — заглушка |
| AI Briefing | 90% | Race condition multi-instance |
