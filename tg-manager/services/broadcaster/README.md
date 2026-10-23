# services/broadcaster

Модуль **Broadcaster** — фоновый исполнитель рассылок с rate-limiting, трекингом
прогресса, шаблонной рендерингом и A/B тестированием.

Обеспечивает отправку broadcast-сообщений нескольким пользователям с:
- Rate limiting (настраиваемая задержка между сообщениями)
- Трекинг прогресса и статусные обновления
- Рендеринг плейсхолдеров шаблонов
- Проверки контента на безопасность
- Валидация входных данных и защита от инъекций

## Основные функции

### run()
Основная функция запуска рассылки. Принимает broadcast_id, token, bot_id, text,
опционально photo_file_id, user_ids, buttons, start_delay, silent. Выполняет
валидацию входных данных, проверку контента, pre-flight проверку токена через
getMe, пропуск уже доставленных пользователей (crash-resume), рендеринг
плейсхолдеров, отправку с rate-limiting, трекинг прогресса в БД.

### start()
Обёртка для запуска run() как asyncio.Task. Регистрирует задачу в _running
дикте для возможности отмены.

### resume_interrupted()
Перезапуск рассылок, оборвавшихся на рестарте процесса. Загружает из БД
рассылки в статусе running/pending и перезапускает их с пропуском уже
доставленных пользователей.

### cancel(broadcast_id)
Отмена активной рассылки по broadcast_id. Возвращает True если задача найдена
и отменена.

### is_running(broadcast_id)
Проверка, запущена ли рассылка. Возвращает True если задача активна.

### mass_broadcast_with_scheduling()
Создание рассылки с расписанием (отложенная отправка). Поддерживает
schedule_minutes (отложенная отправка через N минут) или scheduled_for (конкретная
дата/время). Создаёт operation_queue для планировщика.

### ab_test_broadcast()
A/B тестирование рассылок. Принимает список вариантов [{text, weight}],
делит аудиторию пропорционально весам, создаёт отдельные рассылки для каждого
варианта.

### get_broadcast_analytics()
Аналитика рассылки: статус, доставка, клики, ошибки. Возвращает сводку по
рассылке с проверкой владения, включая delivery_rate_pct и delivery_hourly.

## API эндпоинты

Все эндпоинты доступны через Mini App API (`/api/miniapp/broadcast/...`).

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| POST | `/broadcast/start` | Запуск рассылки |
| GET | `/broadcast/{id}` | Статус рассылки |
| POST | `/broadcast/{id}/cancel` | Отмена рассылки |
| GET | `/broadcast/{id}/analytics` | Аналитика рассылки |
| POST | `/broadcast/schedule` | Создание отложенной рассылки |
| POST | `/broadcast/ab-test` | A/B тестирование |

## Примеры использования

```python
from services.broadcaster import (
    run, start, resume_interrupted, cancel, is_running,
    mass_broadcast_with_scheduling, ab_test_broadcast, get_broadcast_analytics,
)

# Запуск рассылки напрямую
await run(pool, session, broadcast_id=123, token="BOT_TOKEN", bot_id=456,
          text="Привет, {{USERNAME}}!", user_ids=[111, 222, 333])

# Запуск как фоновая задача
start(pool, session, broadcast_id=123, token="BOT_TOKEN", bot_id=456,
      text="Привет!", user_ids=[111, 222])

# Перезапуск прерванных рассылок после рестарта
await resume_interrupted(pool)

# Отмена активной рассылки
cancelled = cancel(broadcast_id=123)

# Проверка статуса
running = is_running(broadcast_id=123)

# Отложенная рассылка через 30 минут
result = await mass_broadcast_with_scheduling(
    pool, owner_id=1, bot_id=456, text="Отложенное сообщение",
    schedule={"schedule_minutes": 30}
)

# A/B тестирование
result = await ab_test_broadcast(
    pool, owner_id=1, bot_id=456,
    variants=[
        {"text": "Вариант A: Привет!", "weight": 1},
        {"text": "Вариант B: Здравствуйте!", "weight": 1},
    ]
)

# Аналитика рассылки
analytics = await get_broadcast_analytics(pool, owner_id=1, broadcast_id=123)
print(f"Доставлено: {analytics['sent_count']}/{analytics['total_users']}")
```

## Зависимости

- `asyncpg` — PostgreSQL-пул
- `aiohttp` — HTTP-сессия для отправки сообщений
- `services.bot_api` — Telegram Bot API клиент
- `services.brand_injection` — инъекция промо для free-tier ботов
- `services.content_safety` — проверка контента
- `services.cache` — TTL-кэш для результатов
- `services.security` — валидация входных данных
- `bot.utils.template_validator` — рендеринг плейсхолдеров