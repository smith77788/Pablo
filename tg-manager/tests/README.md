# Тесты

Быстрые регресс-тесты без реального Postgres/Telethon (стабы в `conftest.py`).

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

Покрытие:
- `test_imports.py` — smoke: импорт всех хендлеров/сервисов + `main` (ловит поломки старта).
- `test_op_types.py` — каждый op_type из очереди диспатчится воркером (защита от «зависших» операций).
- `test_settings.py` — AI-ключи и кошельки: приоритет БД-override над env + fallback.
- `test_logic.py` — классификатор ниш, извлечение упавших каналов, разбор результата INSERT.
