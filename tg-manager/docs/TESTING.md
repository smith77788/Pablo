# Testing — Тестирование

## Типы тестов

### 1. Syntax Check (обязательно)

```bash
# Все .py файлы
python3 -c "import ast; ast.parse(open('file.py').read())"

# Массовая проверка
for f in services/*.py bot/handlers/*.py; do
  python3 -c "import ast; ast.parse(open('$f').read())" && echo "$f: OK" || echo "$f: FAIL"
done
```

### 2. Protected DB Calls

```bash
# Нет unprotected pool-вызовов
grep -n "await pool\." services/op_worker.py | grep -v "_safe_" | wc -l
# Должно быть: 0
```

### 3. Navigation Buttons

```bash
# Нет сообщений без кнопок
grep -rn "edit_text.*Ошибка\|edit_text.*не найден" bot/handlers/ | grep -v "kb\|markup" | wc -l
# Должно быть: 0
```

---

## Ручные тесты

### Бот

1. `/start` — главное меню открывается
2. Нажать каждую кнопку — нет ошибок
3. FSM-флоу — Cancel работает
4. Операция — прогресс отображается

### Mini App

1. Открывается без ошибок
2. Dashboard загружается
3. SSE подключается (🟢)
4. Операции отображаются

---

## Автоматические тесты

### CI (pytest)

```bash
# Запуск тестов
pytest tests/

# Только критические
pytest tests/ -k "critical"
```

### Тестовые сценарии

| Сценарий | Ожидаемый результат |
|----------|---------------------|
| Создание операции | op_id возвращён |
| Circuit Breaker trip | операции пропускаются |
| Session Health Monitor | статус обновляется |
| Proxy fail | auto-switch |
| FloodWait | ожидание + retry |

---

## Мониторинг после деплоя

### Метрики

- Operation success rate
- Op_worker crashes
- Proxy error rate
- API response time
- Cache hit rate
- Circuit breaker trips

### Алерты

- `utilization > 80%` — высокая нагрузка на пул
- `circuit_breaker tripped` — автопауза
- `session_health: status changed` — смена статуса аккаунта
