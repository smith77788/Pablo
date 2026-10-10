# Security — Безопасность

## Принципы
1. Владелец изолирован — только свои данные
2. Нет IDOR — все запросы проверяют owner_id
3. Нет plaintext секретов
4. Rate limiting
5. Валидация ввода

## Аутентификация
```python
# Mini App: JWT
uid = _get_uid(request)
if not uid: return _err("Unauthorized", 401)

# Bot: owner_id из callback
owner_id = callback.from_user.id
```

## Owner Scope
```python
row = await pool.fetchrow(
    "SELECT * FROM table WHERE id=$1 AND owner_id=$2",
    item_id, owner_id,
)
```

## Валидация
```python
# Параметры
target = (body.get("target") or "").strip()
if not target: return _err("Required", 400)

# Callback data
parts = callback.data.split(":")
if len(parts) < 3: return
```

## Rate Limiting
```python
_times: dict[int, list[float]] = {}
def check_rate_limit(uid: int) -> bool:
    now = time.time()
    times = _times.setdefault(uid, [])
    times[:] = [t for t in times if now - t < 60]
    if len(times) >= 10: return False
    times.append(now)
    return True
```

## Запреты
- ❌ Токены в коде/URL
- ❌ `exec()` с пользовательским вводом
- ❌ Логирование паролей
