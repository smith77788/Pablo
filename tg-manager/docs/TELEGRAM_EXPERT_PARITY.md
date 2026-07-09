# Telegram Expert — матрица паритета (выверено по коду)

Источник истины охвата паритета. Обновляй при закрытии пробела. Статусы:
**WIRED** — есть mini-app маршрут + исполнитель (реально работает);
**BACKEND** — логика в Python есть, но UI/подключение неполное (риск «мёртвой оболочки» — проверять по правилу №1);
**НЕТ** — отсутствует.

> Ключевое наблюдение (подтверждено): почти все модули используют ОДИН конвейер —
> источник данных → выбор аккаунтов → лимиты → FloodWait → рандомизация → потоки →
> лог → отчёт. У нас он уже реализован: `operation_queue`+`op_worker` (потоки/floodwait/
> лог/отчёт), `resource_selector` (выбор аккаунтов), `session_simulator` (рандомизация/
> задержки), `account_budget` (лимиты). Пробелы — это конкретные **op_type/модули**, а не
> инфраструктура.

## Крупные РЕАЛЬНЫЕ пробелы (подтверждено grep по коду)
| Модуль | Раздел TE | Статус | Примечание |
|---|---|---|---|
| Проверка уникальности IP прокси | 13 Прокси | **НЕТ → в работе (этот заход)** | ядро изоляции: два активных аккаунта на одном IP = риск бана |
| Автопостинг в чаты v1/v2 | 7 Отправка | НЕТ | join по ключам + постинг циклом; конвейер есть |
| Session Duplicator | 12 Спец | НЕТ | доп. авторизованная сессия для ротации/бэкапа |
| Chat Cloner | 12 Спец | **WIRED (единый модуль)** | НЕ отдельный модуль (был бы дубль). Единый `content_cloner_engine` целе-агностичен: `get_entity`+`forward_messages`/copy работают и с группами; `parse_channel_ref` принимает групповые @username/id/invite. UI Контент-клонера явно поддерживает «канал/группу». Тест: `test_content_cloner_groups.py` |
| Shadow Sessions | 12 Спец | НЕТ | |
| AI Commenting | 12 Спец | НЕТ | GPT-комментинг в обсуждениях |
| Global Search | 12 Спец | **WIRED** | `global_search_engine.search_public` (contacts.SearchRequest) + POST `/api/miniapp/global_search` + экран `s-gsearch`, тайл «Глобал. поиск» |
| Story Manager | 12 Спец / 3 | **WIRED** | `story_manager.post_story` (stories.SendStoryRequest, фото/видео по URL, CanSendStory-проверка, период 6/12/24/48ч) + POST `/account/{id}/post_story` + кнопка «📸 История». Публикация ТОЛЬКО на свой аккаунт. Тест `test_story_manager.py` |
| Flash Call / Voice reg | 4 Авто-рег | НЕТ | сейчас только SMS-коды |
| Backup Proxy | 13 Прокси | **WIRED** | `proxy_selector.failover_dead_proxies` (пробит + переназначение на живой резерв с IP-изоляцией) + POST `/proxy/failover` + `/proxy/{id}/backup` + кнопка «🛟 Failover» и переключатель резерва в списке прокси; `is_backup` (schema_v150). Попутно: `probe_proxy`/`check_proxy_health` теперь расшифровывают proxy_url (был баг — прокси всегда «мёртв») |
| Message Interceptor | 12 Спец | НЕТ | (auto_responder ≠ перехват входящих) |

## Разделы, где база ЕСТЬ (аккаунт-операции активно добивает параллельный агент, waves 1-2)
- **2 Панель аккаунтов**: категории, массовая проверка (бан/огранич), массовые действия (фото/имя/username/bio/2FA/close_sessions/online/приватность/прокси/роли), JSON generator/export, поиск/фильтр/импорт. Move-between-folders + set gender — backlog агента.
- **3 Действие с аккаунтом**: проверено wiring (правило №1):
  - **WIRED**: Поиск админ-чатов (`scan` → op `scan_owned_resources` + `_exec_scan_owned_resources`), Массовые отписки/Выход из чатов (`leave_all` → op `leave_all_chats` + реальный `client.delete_dialog` цикл), обе кнопки в UI + `accAction` c `pollOpResult`.
  - **WIRED (новое)**: Снятие спамблока — `account_manager.appeal_spamblock` (проход по кнопкам аппеляции @SpamBot) + POST `/account/{id}/spamblock_appeal` + кнопка «🛡 Снять спамблок» в карточке аккаунта. Раньше был только CHECK, снятия не было.
  - **WIRED (новое)**: Чтение диалогов — op `read_all_dialogs` + `_exec_read_all_dialogs` (реальный `send_read_acknowledge` только по непрочитанным) + `accAction(read_all)` + кнопка «📖 Прочитать всё».
  - **WIRED**: Создание чатов/ботов (op `create_channel`/`create_group`/`bot_factory` + исполнители), Экспорт аккаунта (`/accounts/export` CSV + `/export_json`).
  - Остальное (удаление истории диалогов, Story Manager) — добивается.
- **4 Авто-регистрация**: генератор device-параметров, SMS-провайдеры (`sms_api_engine`), авто-рег — есть; Flash Call/Voice — НЕТ.
- **5 Сбор аудитории**: parser (`/parser/*`) — WIRED.
- **6 Инвайт**: mass_invite (op) — WIRED; Invite V2/через ботов/пакетами — проверять.
- **7 Отправка**: dm_campaign — WIRED; автопостинг v1/v2 — НЕТ; спинтакс/рандомайзер — есть.
- **10 Накрутка**: boost_views/reactions/stories/subscribers — WIRED.
- **12 Спец**: TDesktop конвертер (`session_converter`), Booster, Forwarder/Reporter (`strike_engine`/`content_cloner`), Channel Cloner — BACKEND; остальное см. пробелы.
- **13 Прокси**: user_proxies + проверка/гео — есть; IP-уникальность/Sticky/Rotating/Backup — НЕТ/частично.

## Правило координации
Аккаунт-операции (разделы 2-3 БЕЗОПАСНОСТЬ/НАСТРОЙКА/JSON) — за параллельным агентом (см. ledger waves). Чтобы не сталкиваться на `mini_app_api.py`/`index.html`, брать модули из списка «крупных пробелов», предпочтительно в отдельных сервисах.
