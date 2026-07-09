# PROJECT AUDIT — Infragram / Telegram Management Platform

Этап 1 аудита (карта проекта). Основано на фактической инвентаризации кода
(2026-07-09), а не на вижн-документах. Легенда статусов:

- ✅ **реализовано** — работает, покрывает основной сценарий
- 🟡 **частично** — есть, но не доведено / не полный сценарий
- ⛔ **отсутствует** — не найдено в коде
- ♻️ **требует переработки** — есть, но с архитектурными/качественными проблемами
- ⚡ **требует оптимизации** — работает, но медленно/тяжело

Метки детекции: **R** = REST-маршрут в `mini_app_api`, **S** = сервис,
**H** = бот-хендлер, **U** = экран/логика во фронте (`mini_app/index.html`).

---

## 0. Архитектура и стек (факт)

| Слой | Что | Объём |
|------|-----|-------|
| Bot | aiogram 3.x, Router-хендлеры, FSM, CallbackData | 91 файл `bot/handlers/` |
| Userbot | Telethon (MTProto), прокси-изоляция под аккаунт, device-fingerprint | `account_manager.py` (ядро) |
| Backend API | aiohttp REST для Mini App | **312 маршрутов** в `services/mini_app_api.py` (11 264 строки) |
| Frontend | Один файл Mini App (HTML+CSS+JS) | `mini_app/index.html` (13 505 строк, **120 экранов**) |
| Task system | `operation_queue` (Postgres) + `op_worker` воркер + `operation_bus` реестр | **~55 op_type**, `op_worker.py` 8 750 строк |
| Данные | asyncpg + PostgreSQL, ручные миграции | **151** файл `schema*.sql` |
| Сервисы | Движки (flood, physics, pacing, strike, dm, broadcaster, health…) | **123** файла `services/` |
| Тесты | pytest, стабы тяжёлых зависимостей | **60** файлов `tests/` |

**Оценка архитектуры:** зрелая, модульная на backend (сервис-на-домен),
task-система единообразна (enqueue → op_worker dispatch → executor). Главный
архитектурный долг — **монолитный `index.html` на 13,5к строк** (весь UI/JS/CSS
вперемешку) и **бесконтрольный рост схемы** (151 миграция). Это НЕ повод для
переписывания — рефакторить точечно при касании.

---

## 1. Основные модули (1–40)

| # | Модуль | Метки | Статус | Комментарий |
|---|--------|-------|--------|-------------|
| 1 | Dashboard | R-HU | ✅ | KPI-лента, быстрые действия, мини-график активности |
| 2 | Task Manager | RS-U | ✅ | `operation_queue` + `op_worker`; экран операций, отмена/повтор |
| 3 | Account Manager | RSHU | ✅ | список/детали, health-фильтры, CRM-статусы (stage), кластеры |
| 4 | Account Actions | RSHU | ✅ | 12 профильных op (имя/аватар/2FA/username/privacy/…), единый `apply_op` |
| 5 | Registration | R-HU | ✅ | `auto_registrar` + `sms_api_engine`, device-fingerprint, прокси на регу |
| 6 | Audience Collection | RSHU | ✅ | парсер аудитории, `niche_searcher`, фильтры (15 маршрутов) |
| 7 | Invite | RSHU | ✅ | `mass_inviter`, чанкинг по аккаунтам, exclude-ids |
| 8 | Messaging | RSHU | ✅ | DM-кампании (`dm_engine`), сетевые рассылки (`broadcaster`) |
| 9 | Contacts | RS-U | ✅⚡ | `contacts_hub` (14 модулей, 58 маршрутов); синхронизация чинилась (параллель + диагностика) |
| 10 | Phone Numbers | R-HU | 🟡 | SMS/номера есть в `sms_api_engine`, но привязаны к регистрации — нет отдельного экрана управления номерами |
| 11 | Reports | RSHU | ✅ | `reporter_engine`, Strike (история с метриками) |
| 12 | Proxy Manager | RSHU | ✅ | добавление/bulk-импорт/проверка уникальности IP, шифрование URL |
| 13 | Proxy Marketplace | ---- | ⛔ | **Отсутствует** — раздел «Прокси-партнёры/маркетплейс» не реализован |
| 14 | Database | RS-- | ✅ | export/import, доступ к данным; UI-раздел «Базы данных» ограничен |
| 15 | Text Editor | RSHU | ✅ | Spintax-модуль (`spintax_service` + LLM-синонимы) |
| 16 | AI Assistant | RSHU | ✅ | ИИ-память, ассистент в contacts_hub |
| 17 | GPT | -SH- | ✅ | `spintax_ai` (OpenRouter/Groq/Gemini/Ollama failover) — как слой, не отдельный экран |
| 18 | Randomizer | ---U | ✅ | «Генератор параметров» устройства (manufacturer/app-version), spintax-рандомизация |
| 19 | Statistics | RSHU | ✅ | аналитика, `physics_engine` (риск-скоры), телеметрия |
| 20 | Settings | R--U | 🟡 | есть, но разрознены по экранам — нет единого центра настроек |
| 21 | Plugins | ---- | ⛔ | **Отсутствует** — системы плагинов нет |
| 22 | Integrations | RSHU | ✅ | SMM-панели (`/promo`), payment webhook, bot webhook |
| 23 | Scheduler | RSHU | ✅ | расписания постов/операций |
| 24 | Notifications | -S-U | 🟡 | бэкенд-уведомления есть (deploy_notifier, monitor-алерты), единого Notification Center нет |
| 25 | Logs | RS-U | ✅ | operation_log (per-target, недавно оживлён в UI), audit trail |
| 26 | Security | RS-U | ✅♻️ | `token_vault` (AES-GCM) шифрует bot-токены; **session_str/proxy_url в plaintext** — критический долг |
| 27 | Roles | R-HU | 🟡 | workspaces + workspace_members есть; полноценной ролевой модели (RBAC) нет |
| 28 | Permissions | R--U | 🟡 | планы/подписки как гейтинг; тонких прав нет |
| 29 | Themes | ---U | 🟡 | дизайн-токены (`--bg` и т.д.) есть, но **нет переключателя light/dark** — фиксированная тёмная |
| 30 | Backup | R--U | 🟡 | экспорт данных есть; полноценного backup/restore-центра нет |
| 31 | Import | RS-U | ✅ | `session_importer` (валидация), импорт аккаунтов/каналов |
| 32 | Export | R--U | ✅ | CSV/JSON экспорт (аккаунты, контакты, операции) — 9 маршрутов |
| 33 | Search | RS-U | ✅ | поиск контактов (spotlight), Global Search публичных сущностей |
| 34 | Filters | ---U | ✅ | health-фильтры + stage-срез аккаунтов, фильтры аудитории/контактов |
| 35 | Queue | RS-U | ✅ | `operation_queue` + `operation_bus` реестр |
| 36 | Workers | -S-U | ✅ | `op_worker` (параллельный режим) |
| 37 | Multi-threading | -S-- | ✅ | параллелизм в op_worker/flood_engine (Semaphore-конкурентность) |
| 38 | FloodWait manager | -S-U | ✅ | `flood_engine`, `pacing_engine`, adaptive delay |
| 39 | Error Recovery | RS-U | ✅ | retry операций, реактивация аккаунтов, `account_health` |
| 40 | Session Manager | RS-U | ✅♻️ | `account_manager` (ядро сессий); долг — plaintext session_str |

---

## 2. Специальные модули

| Модуль | Метки | Статус | Комментарий |
|--------|-------|--------|-------------|
| Session Converter | -S-U | ✅ | `session_converter` (tdata/session) |
| Session Duplicator | R--U | 🟡 | частично — упоминается, но не полноценный дубликатор |
| Forwarder | ---- | 🟡 | классического «переслать из A в B» нет; есть `content_mesh` (распределение контента) |
| Chat Clone | RSHU | ✅ | `content_cloner_engine` (клон групп) |
| Channel Clone | RSH- | ✅ | клонирование каналов через Content Cloner |
| Auto Reply | RSHU | ✅ | `auto_responder` (окна, match_mode) |
| Auto Posting | -SH- | ✅ | `content_mesh` + op `bulk_post_chans`/`quick_post`/`pin_last_post` |
| Story Manager | R--U | 🟡 | накрутка просмотров сторис (`boost_stories`) есть; **постинга своих сторис нет** |
| Contact Book | RS-- | ✅ | = `contacts_hub` (55 маршрутов) |
| AI Commenting | ---U | ⛔ | **Отсутствует** — авто-комментирование не реализовано |
| Global Search | RS-U | ✅ | глобальный поиск публичных сущностей |
| JSON Generator | R--U | ✅ | экспорт метаданных аккаунтов в JSON (без секретов) |
| Device Generator | -S-- | ✅ | `generate_device_fingerprint`, профили устройств (schema_v149) |
| Bulk Editor | RSHU | ✅ | массовое редактирование каналов/ботов |
| Mass Actions | RSHU | ✅ | 5 действий над аккаунтами × 12 профильных op; per-account лог |
| Smart Filters | R--U | 🟡 | базовые фильтры есть; «умных» правил (contacts smart-tags) частично |
| Proxy Checker | RSHU | ✅ | проверка уникальности IP, изоляция, alive-проверка |
| Proxy Rotation | -S-U | 🟡 | `proxy_selector` выбирает/скорит, автоматической ротации по расписанию нет |
| Account Health | RS-U | ✅ | `account_health`, снапшоты, health-dashboard |
| Session Health | RS-U | ✅ | check_account_status_full, реактивация |
| Notification Center | -S-U | 🟡 | см. Notifications — единого центра нет |
| Update Center | R--U | 🟡 | версия/деплой-нотификатор есть; changelog-центра нет |

---

## 3. Приоритетные находки для Этапа 2 (анализ качества)

Найдено в ходе этой и предыдущих сессий (детали — в `AUDIT_LEDGER.md`):

**Реальные баги класса «несуществующая колонка» (уже исправлены):** admin_users,
admin_user_detail, team_members (экран «Команда» 500), ecosystem_members ×6,
`db.py` get_bot/get_bots (корень ошибки Audience DNA). — *паттерн: сверять SQL с
реальной схемой.*

**Мёртвые кнопки / невидимые данные (исправлены):** маршрут лога операции,
Strike-история читала не тот источник (метрики были невидимы), scan копил
детализацию и выбрасывал её.

**Открытый критический долг (требует плана, НЕ наскока):**
- ♻️ **Plaintext `session_str` и `proxy_url`** в БД (при декларации шифрования) —
  ~30+ точек чтения, горячий путь. Нужен план миграции + decrypt-on-read.
- ⚡ **`index.html` 13,5к строк** — тяжёлый первый рендер, рискованные правки.
  Выносить экраны при касании, без переписывания.
- ⚡ **151 миграция** — неконтролируемый рост схемы; консолидация — отдельная тема.

**UX-несогласованность (Этап UX):** экраны построены по-разному (нет единого
Header→Toolbar→Filters→Content→Actions→Logs→StatusBar); формы/модалки местами
различаются (уже точечно чинилось: modal-bg→mbg и т.п.).

---

## 4. Явные пробелы (кандидаты на добавление — Этап 3)

Только то, чего объективно НЕТ (не дублировать существующее):

1. ⛔ **Proxy Marketplace / Прокси-партнёры** — раздел из конкурентного дерева.
2. ⛔ **Plugins** — система расширений.
3. ⛔ **AI Commenting** — авто-комментирование постов.
4. 🟡 **Notification Center** — единый UI поверх существующих бэкенд-уведомлений.
5. 🟡 **Story posting** — публикация своих сторис (есть только накрутка просмотров).
6. 🟡 **Theme switcher** — light/dark переключатель (токены уже есть).
7. 🟡 **Единый центр настроек** (Settings) и **Backup/Restore-центр**.
8. 🟡 **RBAC (роли/права)** поверх workspaces.
9. 🟡 **Phone Numbers** — отдельный экран управления номерами.

---

## 5. Вывод Этапа 1

Проект — **зрелая enterprise-платформа**, а не заготовка: 312 API, 120 экранов,
55 типов задач, 123 сервиса. ~80% заявленных модулей **реализованы**. Стратегия
дальше — **рефакторинг и доведение**, не переписывание:

1. **Этап 2** — вычистить костыли/дубли/баги по приоритету выше (SQL-класс уже
   почти закрыт; далее — UX-консистентность и plaintext-секреты по плану).
2. **Этап 3** — доделать 🟡-модули и добавить ⛔-пробелы, встраивая в текущую
   архитектуру (op_worker/operation_bus/mini_app_api), без параллельных систем.

> Ограничение по этике: наступательные модули (Strike и т.п.) доводятся по
> технической корректности и видимости, но **без наращивания takedown-
> эффективности через ложные жалобы** — см. запись в `AUDIT_LEDGER.md`.
