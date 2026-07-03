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

## Поток операции
```
Handler → operation_bus.submit() → op_worker → _exec_*() → result
Progress Monitor: каждые 15сек, milestone 25/50/75%
```

## op_worker
- 53 op_type, 50 exec функций
- Circuit Breaker: 3 ошибки → 30мин cooldown
- Adaptive Pacing: learning из истории
- Smart Retry: FloodWait/PeerFlood/AUTH_KEY
