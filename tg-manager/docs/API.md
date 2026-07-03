# API — Mini App API

## Обзор

REST API для Telegram Mini App. Все endpoints в `services/mini_app_api.py`.

---

## Аутентификация

### JWT Token

```python
# Все запросы требуют токен
Authorization: Bearer <jwt_token>

# Получение токена
POST /api/miniapp/auth
Body: { "initData": "telegram_webapp_init_data" }
Response: { "token": "eyJ..." }
```

### Публичные endpoints (без auth)

- `GET /api/miniapp/config` — конфигурация
- `POST /api/miniapp/auth` — аутентификация
- `GET /api/miniapp/health` — health check

---

## Endpoints

### Dashboard

```
GET /api/miniapp/dashboard
Response: {
  "stats": { "bots": 5, "channels": 12, "operations": 150 },
  "activity": [...],
  "quick_actions": [...]
}
```

### Accounts

```
GET    /api/miniapp/accounts          — список
POST   /api/miniapp/account/add       — добавить
DELETE /api/miniapp/account/{id}      — удалить
GET    /api/miniapp/account/{id}      — детали
```

### Channels

```
GET    /api/miniapp/channels          — список
POST   /api/miniapp/channel/add       — добавить
DELETE /api/miniapp/channel/{id}      — удалить
GET    /api/miniapp/channel/{id}      — детали
```

### Bots

```
GET    /api/miniapp/bots              — список
POST   /api/miniapp/bot/add           — добавить
DELETE /api/miniapp/bot/{id}          — удалить
GET    /api/miniapp/bot/{id}          — детали
```

### Operations

```
GET    /api/miniapp/operations        — очередь
POST   /api/miniapp/operations        — создать
GET    /api/miniapp/operations/{id}   — детали
POST   /api/miniapp/operations/{id}/cancel — отменить
```

### Ecosystems

```
GET    /api/miniapp/ecosystems        — список
POST   /api/miniapp/ecosystem/create  — создать
GET    /api/miniapp/ecosystem/{id}    — детали
POST   /api/miniapp/ecosystem/{id}/add-member — добавить участника
```

### SSE Events

```
GET /api/miniapp/events?token=<jwt>

Events:
- stats         — dashboard stats (каждые 15сек)
- activity      — последние операции (каждые 15сек)
- op_progress   — running операции (каждые 15сек)
```

---

## Ошибки

### Формат

```json
{
  "error": "error message",
  "detail": "detailed description"
}
```

### Коды

| Код | Описание |
|-----|----------|
| 200 | OK |
| 400 | Bad Request (невалидные параметры) |
| 401 | Unauthorized (нет токена) |
| 404 | Not Found |
| 500 | Internal Server Error |

---

## Rate Limiting

- Auth: 5 requests/minute
- Dashboard: 30 requests/minute
- Operations: 10 requests/minute
