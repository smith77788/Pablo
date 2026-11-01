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

## Ссылки без заголовка (SSE, выгрузки, downloadFile)
Токен сессии в строку запроса не кладём: он оседает в access-логе, истории
браузера и логах прокси. Клиент берёт пропуск на один путь
(`POST /api/miniapp/url_ticket`) и передаёт `?ticket=`; `?token=` сервер не
принимает. HTTP-сервер пишет access-лог через `RedactingAccessLogger` —
значения `token`, `ticket`, `initData`, ключей и подписей заменены на `***`.

## Тревоги владельцу
Падение фонового сервиса и всплеск ошибок уходят админам платформы через
`services/platform_alert.py` (D27).

## Проход 2026-11-01
Что проверено и чем закреплено (тесты — в `RELEASE_GATES.md`, раздел 5):
- `exec`/`eval` в коде продукта — нет; `subprocess` — только `git` с
  фиксированными аргументами (`deploy_notifier`), `shell=True` нет.
- Токенов бота в репозитории вне `tests/` — нет.
- SQL через f-строку — пять мест, имена таблиц из констант кода, не из
  ввода (`db_backup`, диагностика в `start.py`, `lock_timeout` в `db.py`);
  `db_optimizer.analyze_table_usage` ни откуда не вызывается.
- Токен в URL (D19) — закрыт пропуском; access-лог маскирует секреты;
  выгрузка контактов, клавшая токен в адрес, переведена на пропуск.
- Остаётся открытым: внешний пентест перед публичным запуском.

## Запреты
- ❌ Токены в коде/URL
- ❌ `exec()` с пользовательским вводом
- ❌ Логирование паролей
