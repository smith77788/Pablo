# Infragram

Telegram-инфраструктура и массовые операции: аккаунты, каналы, инвайты,
рассылки, парсинг аудитории, антибан-контур.

**Агентам и новым разработчикам — начинать с [`CLAUDE.md`](CLAUDE.md).**

## Структура

| Путь | Что это |
|---|---|
| `tg-manager/` | Весь продукт: бот на aiogram, HTTP-API мини-аппа, исполнитель очереди операций, ~50 фоновых циклов |
| `assistant/` | Личный бот-спутник: отдельный процесс в том же контейнере, своё окружение, на продукт не влияет |
| `docs/adr/` | Принятые архитектурные решения |
| `docs/BRANCH_RESTORE_POINTS.md` | Точки восстановления всех веток на момент консолидации |

Корневые `Dockerfile`, `start-all.sh` и `railway.json` собирают и запускают
`tg-manager` (основной процесс) плюс `assistant` в фоне.

## Ветка

Работа идёт в **`claude/telegram-bot-services-xfAh6`** — она же деплоится.
`main` — зеркало этой ветки: содержимое совпадает, но пушить туда не нужно.
Параллельные ветки не заводим: репозиторий уже пережил 21 ветку от разных
агентов, и в них терялась готовая работа. Что где лежало —
[`docs/BRANCH_RESTORE_POINTS.md`](docs/BRANCH_RESTORE_POINTS.md).

## Запуск

```bash
cd tg-manager
cp .env.example .env          # заполнить credentials
pip install -r requirements.txt
python -m pytest tests/ -q    # ~4500 тестов, ~3 минуты
python main.py
```

### Роли процесса

Один образ поднимается тремя способами — переменная `INFRAGRAM_ROLE`:

| Роль | Что делает | Когда нужна |
|---|---|---|
| `all` (по умолчанию) | Бот, HTTP и все фоновые циклы в одном процессе | Обычный деплой |
| `web` | Только бот и HTTP, без фоновых циклов | Когда мини-апп упирается в GIL, занятый операциями |
| `worker` | Только фоновые циклы, апдейты не забирает | Пара к `web` |

Исполнитель операций должен быть **ровно один**: лимиты флуда живут в памяти
процесса, и `services/replica_guard.py` предупредит, если реплик больше.
Разносить на `web` + `worker` безопасно, поднимать два `worker` — нет.

## Документация

* [`tg-manager/docs/ARCHITECTURE.md`](tg-manager/docs/ARCHITECTURE.md) — слои и потоки данных
* [`tg-manager/docs/DATABASE.md`](tg-manager/docs/DATABASE.md) — схема и миграции
* [`tg-manager/docs/BAN_WEATHER_MODULE.md`](tg-manager/docs/BAN_WEATHER_MODULE.md) — иммунитет флота
* [`tg-manager/docs/AGENT_PROTOCOL.md`](tg-manager/docs/AGENT_PROTOCOL.md) — операционный протокол
* [`tg-manager/.botmother/`](tg-manager/.botmother/) — свод правил продукта

## Другие продукты

В репозитории раньше лежал **BASIC.FOOD** — ИИ-агенты интернет-магазина
зоотоваров. Это другой продукт: с Infragram он не связан ни кодом, ни данными,
ни историей git (у веток нет общего предка). Из ствола он убран, сейчас лежит в
ветке `claude/ai-agents-business-LCLnI` и переезжает в собственный репозиторий —
шаги в [`docs/BASIC_FOOD_SEPARATION.md`](docs/BASIC_FOOD_SEPARATION.md).
