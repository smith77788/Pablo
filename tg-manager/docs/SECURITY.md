# Security — Безопасность

## Принципы

1. **Владелец изолирован** — каждый видит только свои данные
2. **Нет IDOR** — все запросы проверяют owner_id
3. **Нет plaintext секретов** — токены в env vars
4. **Rate limiting** — защита от спама
5. **Валидация ввода** — проверка всех параметров

---

## Аутентификация

### Mini App

```python
# Все запросы проверяют JWT токен
uid = _get_uid(request)
if not uid:
    return _err("Unauthorized", 401)
```

### Telegram Bot

```python
# Все callback проверяют владельца
@router.callback_query(BmCb.filter(F.action == "detail"))
async def cb_detail(callback: CallbackQuery, pool: asyncpg.Pool):
    owner_id = callback.from_user.id
    # Данные фильтруются по owner_id
```

---

## Owner Scope

### Паттерн

```python
# ВСЕГДА фильтруйте по owner_id
row = await pool.fetchrow(
    "SELECT * FROM table WHERE id=$1 AND owner_id=$2",
    item_id, owner_id,
)
if not row:
    return _err("Not found", 404)
```

### Примеры

```python
# Аккаунты
"SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2"

# Каналы
"SELECT * FROM managed_channels WHERE channel_id=$1 AND owner_id=$2"

# Операции
"SELECT * FROM operation_queue WHERE id=$1 AND owner_id=$2"

# Экосистемы
"SELECT * FROM ecosystems WHERE id=$1 AND owner_id=$2"
```

---

## Валидация ввода

### Параметры

```python
# Всегда проверяйте параметры
target = (body.get("target") or "").strip()
if not target:
    return _err("Target required", 400)

try:
    account_id = int(body.get("account_id", 0))
except (ValueError, TypeError):
    return _err("Invalid account_id", 400)
```

### Callback Data

```python
# Проверяйте длину перед индексацией
parts = callback.data.split(":")
if len(parts) < 3:
    return
try:
    item_id = int(parts[2])
except (ValueError, IndexError):
    return
```

---

## Rate Limiting

### Паттерн

```python
# Простой rate limit per user
_user_request_times: dict[int, list[float]] = {}
_RATE_LIMIT = 10  # requests per minute

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    times = _user_request_times.setdefault(user_id, [])
    times[:] = [t for t in times if now - t < 60]
    if len(times) >= _RATE_LIMIT:
        return False
    times.append(now)
    return True
```

---

## Безопасность аккаунтов

### Session Isolation

```python
# Каждый аккаунт использует свой прокси
if proxy_mode == "bound":
    acc_dict = dict(acc)  # с proxy_url
else:
    acc_dict = {**dict(acc), "proxy_url": None, "enforce_proxy": False}
```

### Dead Session Detection

```python
# При ошибке AUTH_KEY — немедленная деактивация
if _is_dead_session_error(err_str):
    await pool.execute(
        "UPDATE tg_accounts SET is_active=FALSE, acc_status='session_expired' WHERE id=$1",
        acc_id,
    )
```

---

## Чего НЕ делать

- ❌ Хранить токены в коде
- ❌ Передавать токены в URL query params
- ❌ Использовать `exec()` с пользовательским вводом
- ❌ Пропускать валидацию "для удобства"
- ❌ Логировать пароли и токены
