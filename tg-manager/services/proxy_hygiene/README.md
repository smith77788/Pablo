# services/proxy_hygiene

Модуль **Proxy Hygiene** — безопасная гигиена пула прокси для больших сеток.

Три задачи оператора с сотнями прокси:
1. НЕ удалить прокси, назначенный аккаунту (FK `tg_accounts.proxy_id` =
   ON DELETE SET NULL → аккаунт молча уходит НАПРЯМУЮ → AUTH_KEY_DUPLICATED,
   самый дорогой класс багов)
2. Массово вычистить подтверждённо-мёртвые НЕназначенные прокси
3. Выгрузить список для аудита без утечки кредов

## Основные функции

### proxy_is_dead(is_active, is_alive)
Определение мёртвого прокси. Мёртвый = подтверждён пробой как недоступный
(`is_alive IS FALSE`) ИЛИ явно деактивирован (`is_active IS FALSE`).
Важно: `is_alive IS NULL` (никогда не проверяли) — НЕ мёртвый. Неизвестность
не равна смерти.

### can_delete_safely(assigned_count)
Проверка безопасности удаления прокси. Удалять безопасно ТОЛЬКО если он не
назначен ни одному аккаунту. Иначе удаление обнулит proxy_id аккаунта
(ON DELETE SET NULL) и аккаунт уйдёт напрямую с домашнего IP → рассинхрон
IP → AUTH_KEY_DUPLICATED.

### is_dead_removable(assigned_count, is_active, is_alive)
Кандидат на авто-вычистку: НЕназначен И подтверждён мёртвым пробой.
Требуем именно `is_alive IS FALSE` (а не просто деактивацию) — чтобы
«очистить мёртвые» удаляло только реально не отвечающие прокси после
проверки, а не временно выключенные оператором.

### mask_proxy_url(url)
Маскировка кредов для экспорта: scheme://user:pass@host:port →
scheme://***@host:port. Хост/порт оставляем (нужны для аудита),
логин/пароль — нет (утечка секретов в CSV недопустима).

### cleanup_dead_proxies(pool, owner_id)
Очистка мёртвых прокси: безопасное удаление подтверждённо-мёртвых
НЕназначенных. Удаляет ТОЛЬКО прокси, которые:
1. Подтверждённо мёртвые (is_alive = FALSE)
2. Не назначены ни одному аккаунту (избегаем ON DELETE SET NULL)

Возвращает {removed_count, removed_ids, skipped_assigned, errors}.

### export_proxies(pool, owner_id, fmt="csv")
Экспорт прокси с маскированием кредов.
- csv: scheme://***@host:port per line (для ручного импорта)
- json: [{id, masked_url, geo_country, is_active, is_alive}]
- list: простой список masked_url

Возвращает {format, count, data: str|list}.

## API эндпоинты

Все эндпоинты доступны через Mini App API (`/api/miniapp/proxy/...`).

| Метод | Эндпоинт | Описание |
|-------|----------|----------|
| POST | `/proxy/cleanup-dead` | Очистка мёртвых прокси |
| GET | `/proxy/export` | Экспорт прокси (csv/json/list) |
| GET | `/proxy/export/csv` | Экспорт в формате CSV |
| GET | `/proxy/export/json` | Экспорт в формате JSON |
| GET | `/proxy/export/list` | Экспорт простым списком |

## Примеры использования

```python
from services.proxy_hygiene import (
    proxy_is_dead, can_delete_safely, is_dead_removable,
    mask_proxy_url, cleanup_dead_proxies, export_proxies,
)

# Проверка мёртвого прокси
is_dead = proxy_is_dead(is_active=False, is_alive=True)  # True
is_dead = proxy_is_dead(is_active=True, is_alive=None)   # False (неизвестно)

# Проверка безопасности удаления
safe = can_delete_safely(assigned_count=0)  # True
safe = can_delete_safely(assigned_count=2)  # False

# Кандидат на авто-вычистку
removable = is_dead_removable(assigned_count=0, is_active=True, is_alive=False)  # True
removable = is_dead_removable(assigned_count=1, is_active=False, is_alive=False)  # False (назначен)

# Маскировка кредов
masked = mask_proxy_url("socks5://user:pass@1.2.3.4:1080")
# → "socks5://***@1.2.3.4:1080"

# Очистка мёртвых прокси
result = await cleanup_dead_proxies(pool, owner_id=1)
print(f"Удалено: {result['removed_count']}, пропущено (назначены): {result['skipped_assigned']}")

# Экспорт прокси в CSV
export = await export_proxies(pool, owner_id=1, fmt="csv")
print(export["data"])  # scheme://***@host:port per line

# Экспорт прокси в JSON
export = await export_proxies(pool, owner_id=1, fmt="json")
for proxy in export["data"]:
    print(f"{proxy['masked_url']}: {proxy['geo_country']}")

# Экспорт прокси простым списком
export = await export_proxies(pool, owner_id=1, fmt="list")
print(export["data"])  # ['scheme://***@host:port', ...]
```

## Зависимости

- `asyncpg` — PostgreSQL-пул
- `services.token_vault` — расшифровка зашифрованных URL прокси