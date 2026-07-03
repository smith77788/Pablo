# Architecture — Архитектура Infragram

## Обзор

```
┌─────────────────────────────────────────────────────┐
│                   ПОЛЬЗОВАТЕЛЬ                        │
│            Telegram Bot · Mini App                    │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                   ОБРАБОТЧИКИ                         │
│  bot/handlers/*.py · services/mini_app_api.py        │
│  operation_bus.submit() → operation_queue             │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                   ДВИЖОК ОПЕРАЦИЙ                     │
│  services/op_worker.py                               │
│  Circuit Breaker · Smart Retry · Progress Monitor    │
│  Adaptive Pacing · Safe DB Helpers                   │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                   СЕРВИСЫ                             │
│  account_manager · strike_engine · ecosystem_brain   │
│  resource_selector · session_simulator · behavioral   │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                   БАЗА ДАННЫХ                         │
│  PostgreSQL · asyncpg pool (15-50 connections)       │
│  Caching Layer · DB Optimizer                        │
└─────────────────────────────────────────────────────┘
```

---

## Ключевые принципы

### 1. Telegram-native

Весь интерфейс — в Telegram. Mini App только для:
- Таблиц > 50 строк
- Сложных графиков
- Топологических карт
- Продвинутой аналитики

### 2. Mass Operations = Продукт

Если человек может сделать в Telegram вручную → Infragram поддерживает:
- На одном объекте
- На_MANY объектах
- По тегу/региону/экосистеме
- С превью и подтверждением
- С безопасным таймингом
- С прогрессом и retry

### 3. Operation Queue

Все действия проходят через `operation_queue`:
- Очередь с приоритетами
- Параллельное выполнение (до 8 операций)
- Circuit Breaker для автопаузы
- Progress Monitor с ETA
- Audit trail для каждого действия

---

## Слои

### Бот Handlers (bot/handlers/)

- 90+ файлов обработчиков
- Каждый экран имеет Cancel/Back кнопки
- FSM-машины с timeout
- `safe_answer()` для всех callback

### Mini App API (services/mini_app_api.py)

- 120+ REST endpoints
- SSE для real-time обновлений
- JWT аутентификация
- Rate limiting

### Operation Engine (services/op_worker.py)

- 53 op_type
- 50 exec функций
- 182 safe DB-хелпера
- Circuit Breaker
- Adaptive Pacing
- Progress Monitor с ETA

### Сервисы (services/)

- account_manager (5225 строк) — управление аккаунтами
- strike_engine (3196 строк) — система жалоб
- ecosystem_brain (1765 строк) — экосистемы
- behavioral_engine (770 строк) — аналитика
- session_simulator (300 строк) — имитация поведения
- cache.py — кэширование
- db_optimizer — оптимизация БД

---

## Поток данных

### Создание операции

```
1. Пользователь нажимает кнопку
2. Handler → operation_bus.submit()
3. INSERT INTO operation_queue (status='pending')
4. op_worker.poll() забирает из очереди
5. _run_op_task() выполняет
6. Progress Monitor отправляет обновления
7. Результат → Telegram уведомление + SSE
```

### Circuit Breaker

```
3+ ошибки подряд → trip (30мин cooldown)
Cooldown истёк → reset (автопродолжение)
Успех → decay (1 ошибка убирается)
```

### Adaptive Pacing

```
История операций → learning
Высокий fail rate → замедление
Низкий fail rate → ускорение
±20% jitter для anti-detection
```
