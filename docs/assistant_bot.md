# Личный ассистент-бот (Telegram ↔ Claude)

Отдельный Telegram-бот для владельца: полноценный чат с Claude плюс доступ ко
всем бизнес-функциям Pablo. Не пересекается с клиентским ботом BASIC.FOOD
(`TELEGRAM_BOT_TOKEN`) — у ассистента свой токен `ASSISTANT_BOT_TOKEN`.

## Возможности

- Диалог с Claude с памятью контекста (переживает рестарты — история в `data/assistant/`)
- Фото → анализ изображения (vision)
- Документы → PDF, изображения и текстовые файлы (txt/md/csv/json/код)
- Бизнес-инструменты, которые Claude вызывает сам: утренний брифинг,
  обработка заказов, остатки склада, недельный отчёт, произвольная аналитика,
  добавление ТТН, оприходование товара
- Быстрые команды: `/briefing`, `/orders`, `/stock`, `/weekly`
- Управление доступом: только админы; `/grant`, `/revoke`, `/admins`
- Смена модели на лету: `/model claude-sonnet-5` и т.п.
- `/new` — очистка контекста, `/status` — состояние бота
- Длинные ответы автоматически режутся под лимит Telegram (4096)

## Запуск

```bash
python main.py assistant     # или: python -m assistant
```

Нужные переменные окружения (`.env` или Railway):

| Переменная | Что это |
|---|---|
| `ASSISTANT_BOT_TOKEN` | токен бота из @BotFather |
| `ANTHROPIC_API_KEY` | ключ Claude API |
| `ASSISTANT_ADMIN_IDS` | (опц.) доп. админы через запятую |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | (опц.) без них бизнес-инструменты вернут ошибку, но чат работает |

Владелец (Telegram ID `391641532`) вшит админом по умолчанию —
`assistant/state.py::DEFAULT_ADMIN_ID`.

## Деплой на Railway (бот 24/7)

Ассистент — **отдельный** сервис (не путать с tg-manager, который собирается
корневым `Dockerfile`). Для него есть свой `assistant.Dockerfile`.

1. Railway → проект → **New Service → GitHub Repo** → этот репозиторий.
2. Service → **Settings → Build**:
   - **Dockerfile Path** = `assistant.Dockerfile`
   - (альтернатива — **Config-as-code Path** = `railway.assistant.json`)
3. Service → **Variables**: добавить `ASSISTANT_BOT_TOKEN` и `ANTHROPIC_API_KEY`
   (при желании `ASSISTANT_ADMIN_IDS`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`).
4. (Опц.) **Volume** на путь `/data` — чтобы история чатов и список админов
   переживали редеплой.
5. **Deploy**. В логах должно появиться `Assistant bot online: @…`, а владельцу
   придёт «🟢 Pablo на связи».

Локальный запуск для проверки:

```bash
export ASSISTANT_BOT_TOKEN=... ANTHROPIC_API_KEY=...
python main.py assistant
```

> ⚠️ Сеть некоторых CI/песочниц блокирует `api.telegram.org`. Запускать бота
> нужно там, где есть доступ к Telegram (Railway или локальная машина).

## Почему бот не падает

Живучесть заложена в трёх уровнях (`assistant/bot.py`):

- **Супервизор** `run()` — оборачивает весь бот в цикл с авто-рестартом и
  экспоненциальным backoff (до 60 c). Падение конструктора (нет
  `ANTHROPIC_API_KEY`, нет сети на старте) или любое необработанное исключение
  → лог + рестарт. Процесс завершается только по Ctrl-C / SIGTERM.
- **Устойчивый старт** `_handshake()` — `deleteWebhook`/`getMe` повторяются с
  backoff, пока Telegram не ответит; сетевой сбой на старте не убивает процесс.
- **Защита цикла опроса** `serve()` — каждая итерация и обработка каждого
  апдейта в `try/except`; ошибка одного сообщения не роняет цикл, пользователю
  уходит «что-то пошло не так».

Поверх этого — `railway.json`/`assistant.Dockerfile` с `restartPolicyType:
ALWAYS`: если контейнер всё же остановится, Railway поднимет его заново.

## Отключение старых интеграций

Если бот раньше был подключён к другому сервису (webhook или чужой polling):

1. При старте бот сам вызывает `deleteWebhook(drop_pending_updates=True)` —
   это снимает webhook и выбрасывает накопившиеся апдейты.
2. Если старый сервис использует **long polling**, Telegram будет отдавать
   409 Conflict — бот это залогирует. Единственный надёжный способ отрезать
   такой сервис: в @BotFather → `/mybots` → бот → API Token → **Revoke**,
   затем прописать новый токен в `ASSISTANT_BOT_TOKEN`.
3. Токен, который уже засветился в чужих сервисах/чатах, стоит отозвать в
   любом случае.

## Архитектура

```
assistant/
  telegram_api.py  — тонкий клиент Bot API (свой токен, long polling, файлы)
  state.py         — админы, модель, истории чатов (JSON в data/assistant/)
  claude_chat.py   — цикл Claude с инструментами (Messages API, adaptive thinking)
  bot.py           — маршрутизация апдейтов, команды, вложения
```

Тесты: `python tests/test_assistant_bot.py`
