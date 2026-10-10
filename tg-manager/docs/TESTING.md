# Testing — Тестирование

## Автоматические проверки
```bash
# Syntax check
python3 -c "import ast; ast.parse(open('file.py').read())"

# Protected DB calls (должно быть 0)
grep -n "await pool\." services/op_worker.py | grep -v "_safe_" | wc -l

# Navigation buttons (должно быть 0)
grep -rn "edit_text.*Ошибка" bot/handlers/ | grep -v "kb\|markup" | wc -l
```

## Ручные тесты
1. `/start` → меню открывается
2. Каждая кнопка → нет ошибок
3. FSM-флоу → Cancel работает
4. Операция → прогресс отображается
5. Mini App → загружается, SSE 🟢

## CI
```bash
pytest tests/
```

## Мониторинг
- Operation success rate
- Op_worker crashes
- Proxy error rate
- API response time
- Circuit breaker trips
