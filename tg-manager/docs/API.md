# API — Mini App API

## Аутентификация
```
POST /api/miniapp/auth → JWT token
Header: Authorization: Bearer <token>
```

## Endpoints

### Dashboard
```
GET /api/miniapp/dashboard → stats, activity, quick_actions
```

### Accounts/Channels/Bots
```
GET    /api/miniapp/{items}        — список
POST   /api/miniapp/{item}/add     — добавить
DELETE /api/miniapp/{item}/{id}    — удалить
GET    /api/miniapp/{item}/{id}    — детали
```

### Operations
```
GET    /api/miniapp/operations        — очередь
POST   /api/miniapp/operations        — создать
POST   /api/miniapp/operations/{id}/cancel — отменить
```

### SSE
```
GET /api/miniapp/events?token=<jwt>
Events: stats, activity, op_progress (каждые 15сек)
```

## Ошибки
```json
{"error": "message", "detail": "description"}
```
| Код | Описание |
|-----|----------|
| 400 | Bad Request |
| 401 | Unauthorized |
| 404 | Not Found |
| 500 | Server Error |
