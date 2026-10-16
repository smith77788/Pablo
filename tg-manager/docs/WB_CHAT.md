# WB Chat — аккаунтная автоматизация (по образцу Telegram-стека)

Infragram автоматизирует Telegram прежде всего на уровне **пользовательских
аккаунтов** через Telethon: сессии, прокси, здоровье, массовые действия (инвайты,
DM, вступления), очередь операций с воркером и ротацией. **WB Chat** — новый
самостоятельный мессенджер Wildberries (приложения из сторов, `chat.wb.ru`),
аналог Telegram. Этот модуль воспроизводит ту же архитектуру для WB Chat.

## Главное ограничение: у WB Chat пока нет публичного API

На момент постройки у мессенджера WB Chat **нет** официального публичного
API/SDK и открытого клиента протокола (в отличие от Telegram, чью автоматизацию
держит зрелый Telethon/MTProto). Проверено по нескольким источникам (август 2026):
мессенджер в бете, вход по WB ID, бот-платформа официально не анонсирована.
Единственные API Wildberries (`dev.wildberries.ru`) — продавцовые (Content/Orders/
Statistics/Advertising и «Чат с покупателями»), это **не** мессенджер.

Поэтому весь аккаунтный слой построен поверх **абстрактного транспорта**
`WBChatTransport` — единственного шва, куда подключается реальный протокол, когда
он появится. Всё выше транспорта — настоящее и зеркалит Telegram.

## Соответствие слоёв

| Слой | Telegram (существующее) | WB Chat (`services/wb_chat/`) |
|---|---|---|
| Клиент протокола | `TelegramClient` / Telethon | `transport.WBChatTransport` + `drivers/` |
| Фабрика клиента | `account_manager._make_client` | `transport.build_transport` / `session_manager` |
| Вход по номеру | phone-login FSM | `login.py` |
| Аккаунты (сессии/прокси/здоровье) | `accounts` + `account_manager` | `accounts.py` + `wb_accounts` |
| Очередь операций + воркер | `op_worker.py` | `op_worker.py` + `wb_operations` |
| Движки масс-действий | `*_engine.py` | `engines/` (эталон: `mass_dm.py`) |
| Пейсинг/предохранитель | `flood_engine.py` | `pacing.py` |
| Шифрование сессий | `token_vault` | тот же `token_vault` |

## Транспорт (`transport.py`, `drivers/`)

`WBChatTransport` — абстрактный класс (что `TelegramClient` для Telegram): одна
сессия одного аккаунта. Методы: `connect/disconnect`, `is_authorized/get_me`,
`export_session`, `start_login/complete_login`, `resolve`, `send_message`,
`join/leave`. Ошибки повторяют иерархию Telethon по смыслу: `WBFloodWait(seconds)`,
`WBPeerInvalid`, `WBAuthError`, `WBProtocolUnavailable`.

Драйверы:
- **`drivers/mock.py`** — в памяти, детерминированный. Позволяет разрабатывать и
  тестировать ВЕСЬ аккаунтный слой без протокола; умеет моделировать flood
  (`flood:*`) и невалидного адресата (`invalid:*`) для проверки ретраев.
- **`drivers/real.py`** — заглушка: каждый метод поднимает `WBProtocolUnavailable`
  с внятным указанием. Точки реализации помечены `TODO(protocol)`. Когда появится
  основа (официальный API или реверс-клиент) — правится ТОЛЬКО этот файл.

Драйвер выбирается `config.WB_CHAT_DRIVER` (`mock` по умолчанию | `real`).

## Аккаунты (`accounts.py`, `wb_accounts`)

Хранилище + ротация. Сессии и прокси шифруются в покое (`token_vault`, как
Telegram-сессии). `pick_account` выбирает активный аккаунт вне кулдауна и в
пределах дневного бюджета, приоритет — «здоровье» ↓ и давность использования ↑.
`penalize` снижает здоровье и уводит в кулдаун после flood/бана. Дневные бюджеты
(`wb_account_budget`) ограничивают действия на аккаунт (защита от банов).

## Вход (`login.py`)

Двухшаговый поток (как phone-login Telegram): `start(phone)` → «отправлен код» →
`complete(pool, token, code)` → сессия экспортируется, шифруется и кладётся в
`wb_accounts`. Между шагами живой транспорт держится в памяти процесса.

## Очередь операций (`op_worker.py`, `wb_operations`/`wb_operation_targets`)

`enqueue(...)` ставит операцию и разворачивает её цели (идемпотентно). Воркер
`run` атомарно забирает queued-операцию (`FOR UPDATE SKIP LOCKED` — безопасно для
нескольких реплик) и исполняет движком из `OP_HANDLERS`. Цели живут отдельными
строками → прогресс перезапускаемый (после сбоя доделываем `pending`).

## Движок mass_dm (`engines/mass_dm.py`)

Эталонная массовая операция: рассылка ЛС с ротацией аккаунтов, пейсингом
(`Pacer`: адаптация на flood + circuit breaker) и дневными бюджетами. Обработка
ошибок транспорта: `flood` → наказать аккаунт и оставить цель на другой аккаунт;
`invalid` → пропустить цель; `protocol-unavailable` → аккуратно провалить операцию
с внятной причиной (а не «зависнуть молча»). Новые op_type добавляются движком +
строкой в `OP_HANDLERS`.

## Конфигурация

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `WB_CHAT_DRIVER` | Драйвер транспорта: `mock` / `real` | `mock` |
| `WB_CHAT_WORKER_ENABLED` | Запускать фоновый воркер операций | `false` |
| `WB_CHAT_DAILY_BUDGET` | Дневной лимит действий на аккаунт | `50` |
| `TOKEN_ENCRYPTION_KEY` | Ключ шифрования сессий (общий с Telegram) | — |

Воркер по умолчанию выключен — до поставки реального протокола нет смысла крутить
пустой цикл в проде. На `mock` его можно включить для отладки.

## Тесты

- `tests/test_wb_chat_transport.py` — контракт транспорта, мок, реальная заглушка (чистые).
- `tests/test_wb_chat_pacing.py` — адаптация задержки, circuit breaker (чистые).
- `tests/test_wb_chat_accounts_postgres.py` — вход, ротация, бюджеты, кулдаун,
  mass_dm end-to-end, protocol-unavailable, эксклюзивный захват — на РЕАЛЬНОМ
  Postgres (гейтингуется `INFRAGRAM_TEST_DSN`, иначе пропускается).

## Что дальше (когда появится протокол)

1. Реализовать `RealWBChatTransport` (`drivers/real.py`) под официальный API WB
   или реверс-клиент — по местам `TODO(protocol)`. Больше нигде правок не нужно.
2. Выставить `WB_CHAT_DRIVER=real`, `WB_CHAT_WORKER_ENABLED=true`.
3. Наращивать движки в `engines/` (bulk_join, invite, broadcast…) и op_type в
   `OP_HANDLERS`, плюс UI/handlers и прогрев аккаунтов — как в Telegram-стеке.
