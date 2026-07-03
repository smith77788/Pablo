# Release Gates — Ворота релиза

## Что проверять перед каждым деплоем

### 1. Синтаксис

```bash
# Все .py файлы должны проходить ast.parse()
python3 -c "import ast; ast.parse(open('file.py').read())"
```

**Обязательно проверять:**
- `services/op_worker.py`
- `services/mini_app_api.py`
- `services/account_manager.py`
- `bot/handlers/*.py` (все хэндлеры)

### 2. Безопасность DB-вызовов

```bash
# Нет unprotected pool-вызовов вне try/except
grep -n "await pool\." services/op_worker.py | grep -v "_safe_" | wc -l
# Должно быть: 0
```

### 3. Кнопки навигации

```bash
# Нет сообщений без кнопок Cancel/Back
grep -rn "edit_text.*Ошибка\|edit_text.*не найден" bot/handlers/ | grep -v "kb\|markup" | wc -l
# Должно быть: 0
```

### 4. Circuit Breaker

```python
# Проверить что circuit breaker активен
from services.op_worker import _circuit_breaker_is_open
assert not _circuit_breaker_is_open(test_owner_id)
```

### 5. Session Health Monitor

```python
# Проверить что монитор запускается
from services.account_manager import run_session_health_monitor
# Должен быть зарегистрирован в main.py
```

---

## Deployment Flow

```
1. git add .
2. git commit -m "feat/fix: описание"
3. git push origin claude/telegram-bot-services-xfAh6
4. Railway auto-deploy (~2-3 мин)
5. Проверить: /version в Telegram боте
6. Проверить: Mini App загружается
```

---

## Post-Deployment Verification

### Бот

- `/start` — главное меню открывается
- `/version` — показывает текущую версию
- Нажать любую кнопку — нет ошибок

### Mini App

- Открывается без ошибок
- Dashboard загружается
- SSE подключается (зелёный индикатор)
- Операции отображаются в реальном времени

### API

```bash
# Health check
curl -H "Authorization: Bearer <token>" https://your-app/api/miniapp/dashboard
# Должен вернуть JSON с stats
```

---

## Rollback Plan

Если после деплоя что-то сломалось:

1. **Быстрый rollback**: `git revert HEAD && git push`
2. **Railway**: предыдущая версия автоматически откатывается
3. **Проверить**: `/version` показывает старую версию

---

## Что НЕ деплоить

- Файлы с секретами (`.env`, токены)
- Временные тестовые файлы
- Файлы с `TODO`/`FIXME` без решения
- Код без syntax check
- Код с unprotected DB-вызовами
