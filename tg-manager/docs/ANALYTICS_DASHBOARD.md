# Analytics Dashboard

## Назначение

Статистика и метрики для дашборда владельца. Предоставляет агрегированную статистику по аккаунтам, операциям, аудитории и доходам для отображения в мини-приложении и уведомлениях.

## Основные функции

- **Общая статистика дашборда** — сводка по всем ресурсам владельца
- **Метрики в реальном времени** — текущее состояние и последние события
- **Исторические данные** — статистика по дням за N дней
- **Экспорт аналитики** — выгрузка данных в JSON или CSV

## API эндпоинты

### Общая статистика

```python
async def get_dashboard_stats(pool: asyncpg.Pool, owner_id: int) -> dict[str, Any]:
    """Общая статистика дашборда — сводка по всем ресурсам владельца.
    
    Возвращает:
        accounts: total, active, banned, spamblock
        operations_24h: total, success, failed, running (за 24ч)
        audience: total_users, new_24h, new_7d
        health: avg_trust, accounts_at_risk
        revenue_30d_usd: estimated_usd (если есть payment данные)
    """
```

### Метрики в реальном времени

```python
async def get_realtime_metrics(pool: asyncpg.Pool, owner_id: int) -> dict[str, Any]:
    """Метрики в реальном времени — текущее состояние и последние события.
    
    Возвращает:
        active_operations: текущие выполняющиеся операции
        recent_events: последние 10 событий (аудит/лог)
        account_status: распределение статусов аккаунтов
        queue_depth: глубина очереди операций
    """
```

### Исторические данные

```python
async def get_historical_data(
    pool: asyncpg.Pool,
    owner_id: int,
    metric: Literal["operations", "accounts", "audience", "health"],
    days: int = 30,
) -> list[dict[str, Any]]:
    """Исторические данные по выбранной метрике за N дней.
    
    Поддерживаемые метрики:
        operations: количество операций по дням
        accounts: количество аккаунтов по дням
        audience: аудитория по дням (изменения)
        health: средний trust_score по дням
    
    Возвращает список {date, value} за каждый день.
    """
```

### Экспорт аналитики

```python
async def export_analytics(
    pool: asyncpg.Pool,
    owner_id: int,
    format: Literal["json", "csv"] = "json",
) -> str:
    """Экспорт полной аналитики в JSON или CSV.
    
    Включает:
        Сводку дашборда
        Метрики в реальном времени
        Историю операций за 30 дней
    """
```

## Примеры использования

```python
# Получение общей статистики
stats = await get_dashboard_stats(pool, owner_id=123)
print(f"Аккаунты: {stats['accounts']['active']} активных")
print(f"Операции за 24ч: {stats['operations_24h']['total']}")
print(f"Новые пользователи за 7д: {stats['audience']['new_7d']}")

# Метрики в реальном времени
realtime = await get_realtime_metrics(pool, owner_id=123)
for op in realtime['active_operations']:
    print(f"Операция #{op['id']}: {op['type']} ({op['status']})")

# Исторические данные по операциям
ops_history = await get_historical_data(pool, owner_id=123, metric="operations", days=7)
for day in ops_history:
    print(f"{day['date']}: {day['total']} операций ({day['success']} успешно)")

# Экспорт в JSON
json_export = await export_analytics(pool, owner_id=123, format="json")
# Сохранение в файл
with open("analytics_export.json", "w") as f:
    f.write(json_export)

# Экспорт в CSV
csv_export = await export_analytics(pool, owner_id=123, format="csv")
print(csv_export)
```

## Структура данных

### Dashboard Stats

```json
{
  "accounts": {
    "total": 15,
    "active": 12,
    "banned": 2,
    "spamblock": 1
  },
  "operations_24h": {
    "total": 45,
    "success": 42,
    "failed": 3,
    "running": 2
  },
  "audience": {
    "total_users": 12500,
    "new_24h": 150,
    "new_7d": 890
  },
  "health": {
    "avg_trust": 0.85,
    "accounts_at_risk": 1
  },
  "revenue_30d_usd": 1250.50
}
```

### Realtime Metrics

```json
{
  "active_operations": [
    {
      "id": 123,
      "type": "mass_invite",
      "status": "running",
      "started_at": "2024-01-15T10:30:00Z",
      "params": {"target": "group_123", "count": 100}
    }
  ],
  "recent_events": [
    {
      "id": 456,
      "action": "invite",
      "result": "success",
      "target": "user_789",
      "occurred_at": "2024-01-15T10:25:00Z"
    }
  ],
  "account_status": {
    "active": 12,
    "banned": 2,
    "spamblock": 1
  },
  "queue_depth": {
    "mass_invite": 5,
    "mass_broadcast": 2
  }
}
```

## Интеграция с Mini App

Данные дашборда отображаются в мини-приложении через API:

```python
# В handlers/mini_app.py
@router.message(F.text == "/dashboard")
async def show_dashboard(message: Message, pool: asyncpg.Pool):
    stats = await get_dashboard_stats(pool, message.from_user.id)
    # Форматирование и отправка в Mini App
```

## Ограничения

- Исторические данные хранятся до 365 дней
- Экспорт в CSV ограничивается 10,000 записями
- Метрики в реальном времени обновляются каждые 30 секунд
- Revenue данные доступны только при подключённой платёжной системе