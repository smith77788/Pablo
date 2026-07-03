# Architecture — Архитектура

## Слои
```
Пользователь (Bot + Mini App)
    ↓
Обработчики (bot/handlers + mini_app_api)
    ↓
Operation Bus → operation_queue
    ↓
op_worker (Circuit Breaker · Smart Retry · Progress)
    ↓
Сервисы (account_manager · strike · ecosystem · ...)
    ↓
PostgreSQL (pool 15-50)
```

## Принципы
1. **Telegram-native** — весь интерфейс в Telegram
2. **Mass Operations** — всё работает в bulk
3. **Operation Queue** — все действия через очередь
4. **Circuit Breaker** — автопауза при 3+ ошибках
5. **Safe DB** — все pool-вызовы защищены

## operation_queue
| Статус | Описание |
|--------|----------|
| pending | Ожидает |
| running | Выполняется |
| done | Завершена |
| failed | Ошибка |
| cancelled | Отменена |

## op_type (основные)
| Тип | Описание |
|-----|----------|
| mass_publish | Публикация в каналы |
| bulk_join/leave | Вступление/выход |
| strike | Жалоба |
| dm_campaign | DM-кампания |
| mass_invite | Инвайт |
| global_presence_* | Создание каналов |
| bot_factory | Создание ботов |

## operation_bus
```python
from services.operation_bus import submit
op_id = await submit(pool, owner_id, "bulk_join", params, total_items=6)
```
Все op_type должны быть в `OP_REGISTRY`.

## op_worker
- 53 op_type, 50 exec функций
- Circuit Breaker: 3 ошибки → 30мин cooldown
- Adaptive Pacing: learning из истории
- Smart Retry: FloodWait/PeerFlood/AUTH_KEY

## Retry Logic
```
FloodWait → ожидание как Telegram просит + 60с jitter
PeerFlood → 48h cooldown + ротация аккаунта
AUTH_KEY dead → немедленная деактивация
CHANNEL_PRIVATE → пропуск канала
Other → exponential backoff ±20% jitter
```

## Progress Monitor
- Проверяет каждые 15 секунд
- Update каждые 30 секунд
- Milestone: 25%, 50%, 75%
- ETA + speed (items/minute)

## Safe DB Helpers
- `_safe_execute()` — INSERT/UPDATE
- `_safe_fetchrow()` — SELECT ONE
- `_safe_fetch()` — SELECT MANY
- `_safe_fetchval()` — SELECT VALUE
