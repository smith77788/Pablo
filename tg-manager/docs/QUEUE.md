# Queue — Очередь операций

## operation_queue

Основная таблица для всех операций.

### Статусы

| Статус | Описание |
|--------|----------|
| `pending` | Ожидает выполнения |
| `running` | Выполняется |
| `done` | Успешно завершена |
| `failed` | Завершена с ошибкой |
| `cancelled` | Отменена пользователем |
| `waiting_approval` | Ожидает подтверждения |

### op_type

| Тип | Описание |
|-----|----------|
| `mass_publish` | Массовая публикация |
| `bulk_join` | Массовое вступление |
| `bulk_leave` | Массовый выход |
| `strike` | Жалоба на контент |
| `dm_campaign` | DM-кампания |
| `mass_invite` | Массовый инвайт |
| `global_presence_*` | Создание каналов/групп |
| `bot_factory` | Создание ботов |
| `content_clone` | Клонирование контента |
| `scan_owned_resources` | Скан ресурсов |

---

## op_worker

Фоновый воркер, обрабатывающий очередь.

### Конфигурация

- `_MAX_PARALLEL = 8` — максимум параллельных операций
- `_MAX_PARALLEL_PER_OWNER = 3` — максимум на владельца
- `_POLL_INTERVAL = 10` — интервал проверки очереди
- `_STALE_RUNNING_TIMEOUT_MIN = 60` — таймаут зависших операций

### Ключевые функции

- `_process_pending()` — забирает задачи из очереди
- `_run_op_task()` — выполняет одну операцию
- `_progress_monitor()` — отслеживает прогресс
- `_maybe_requeue()` — повтор при ошибке
- `_circuit_breaker_*()` — автопауза при сбоях

### Safe DB Helpers

Все pool-вызовы обёрнуты:
- `_safe_execute()` — INSERT/UPDATE
- `_safe_fetchrow()` — SELECT ONE
- `_safe_fetch()` — SELECT MANY
- `_safe_fetchval()` — SELECT VALUE

---

## operation_bus

Модуль для отправки операций в очередь.

### Использование

```python
from services.operation_bus import submit

op_id = await submit(
    pool,
    owner_id,
    "bulk_join",
    {"links": ["@channel1", "@channel2"], "account_ids": [1, 2, 3]},
    total_items=6,
)
```

### OP_REGISTRY

Все op_type должны быть зарегистрированы в `OP_REGISTRY` перед использованием.
