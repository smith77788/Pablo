# Search Ranking Engine

## Назначение

Модуль отслеживания позиций каналов/ботов в результатах поиска Telegram в реальном времени. Хранит историю позиций, отправляет уведомления об изменениях и предлагает рекомендации по оптимизации.

## Основные функции

- **Отслеживание ключевых слов** — автоматическая проверка позиций по заданным запросам
- **Запись позиций** — сохранение результатов проверки с историей изменений
- **Система алертов** — уведомления о значительных изменениях позиций
- **Аналитика** — статистика по отслеживаемым ключевым словам и их позициям
- **Очистка данных** — автоматическое удаление устаревшей истории

## API эндпоинты

### Инициализация таблиц

```python
async def init_ranking_tables(pool: asyncpg.Pool) -> None:
    """Создание таблиц ranking если они не существуют."""
```

### Управление отслеживанием

```python
async def track_keyword(pool: asyncpg.Pool, owner_id: int, keyword: str,
                        channel_id: Optional[int] = None,
                        check_interval: int = 3600) -> dict:
    """Начать отслеживание ключевого слова. Возвращает {'ok': bool, 'id': int}."""

async def untrack_keyword(pool: asyncpg.Pool, owner_id: int, keyword: str,
                          channel_id: Optional[int] = None) -> dict:
    """Прекратить отслеживание ключевого слова."""
```

### Запись позиций

```python
async def record_position(pool: asyncpg.Pool, owner_id: int, channel_id: int,
                          keyword: str, position: int) -> dict:
    """Записать результат проверки позиции. Возвращает {'ok': bool, 'previous': int|None, 'current': int}."""
```

### Получение данных

```python
async def get_position_history(pool: asyncpg.Pool, owner_id: int,
                               channel_id: int, keyword: str,
                               days: int = 30) -> list:
    """Получить историю позиций за N дней."""

async def get_all_positions(pool: asyncpg.Pool, owner_id: int) -> list:
    """Получить последние позиции по всем отслеживаемым ключевым словам."""

async def get_tracked_keywords(pool: asyncpg.Pool, owner_id: int) -> list:
    """Получить список всех отслеживаемых ключевых слов."""

async def get_alerts(pool: asyncpg.Pool, owner_id: int,
                     unacknowledged_only: bool = False) -> list:
    """Получить алерты об изменениях позиций."""

async def acknowledge_alert(pool: asyncpg.Pool, owner_id: int, alert_id: int) -> dict:
    """Подтвердить получение алерта."""
```

### Статистика

```python
async def get_ranking_stats(pool: asyncpg.Pool, owner_id: int) -> dict:
    """Получить статистику отслеживания:
    - total_tracked: количество активных отслеживаний
    - total_checks: общее количество проверок
    - avg_position_7d: средняя позиция за 7 дней
    - alerts_pending: количество не подтверждённых алертов
    """
```

### Очистка

```python
async def cleanup_old_data(pool: asyncpg.Pool, days: int = HISTORY_RETENTION) -> int:
    """Удалить данные старше N дней. Возвращает количество удалённых записей."""
```

## Примеры использования

```python
# Отслеживание ключевого слова
result = await track_keyword(pool, owner_id=123, keyword="crypto", channel_id=-100123456)
# {'ok': True, 'id': 1}

# Запись позиции
result = await record_position(pool, owner_id=123, channel_id=-100123456,
                               keyword="crypto", position=5)
# {'ok': True, 'previous': 8, 'current': 5}

# Получение истории
history = await get_position_history(pool, owner_id=123, channel_id=-100123456,
                                    keyword="crypto", days=30)

# Просмотр статистики
stats = await get_ranking_stats(pool, owner_id=123)
# {'total_tracked': 15, 'total_checks': 720, 'avg_position_7d': 12.3, 'alerts_pending': 2}

# Получение алертов
alerts = await get_alerts(pool, owner_id=123, unacknowledged_only=True)
```

## Конфигурация

- `CHECK_INTERVAL = 3600` — интервал проверки по умолчанию (1 час)
- `HISTORY_RETENTION = 90` — хранение истории (90 дней)
- `ALERT_THRESHOLD = 10` — порог для алертов (изменение позиции > 10)