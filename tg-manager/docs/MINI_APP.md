# Mini App — Telegram Mini App

## Обзор

Single-page приложение для управления Infragram через Telegram WebView.

### Файлы

- `mini_app/index.html` — основной файл (~10000 строк JS)

---

## Архитектура

```
┌─────────────────────────────────────┐
│           Mini App (HTML/JS)         │
│  API calls · SSE · Rendering        │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│        Mini App API                  │
│  /api/miniapp/* endpoints           │
│  JWT auth · Rate limiting           │
└──────────────┬──────────────────────┘
               │
┌──────────────▼──────────────────────┐
│        Database                     │
│  operation_queue · accounts · ...   │
└─────────────────────────────────────┘
```

---

## Ключевые компоненты

### API Calls

```javascript
// Безопасный вызов API
async function api(url, opts = {}) {
  const r = await fetch(url, {
    headers: {'Authorization': 'Bearer ' + TK, 'Content-Type': 'application/json', ...opts.headers},
    ...opts,
  });
  if (!r.ok) { /* обработка ошибки */ }
  try { return await r.json(); } catch(_) { throw new Error('Ответ сервера не является JSON'); }
}
```

### SSE (Server-Sent Events)

```javascript
SSE = new EventSource('/api/miniapp/events?token=' + TK);
SSE.addEventListener('stats', e => { renderCards(JSON.parse(e.data)); });
SSE.addEventListener('activity', e => { renderAct(JSON.parse(e.data).items); });
SSE.addEventListener('op_progress', e => { renderOpProgress(JSON.parse(e.data).items); });
```

### Rendering

- Dashboard: KPI карточки, быстрые действия
- Accounts: список, детали, действия
- Channels: список, детали, bulk операции
- Bots: список, детали, управление
- Operations: очередь, прогресс, история

---

## Экраны

### Главный экран

- KPI: ботов, каналов, операций
- Быстрые действия: добавить бота, канал, операцию
- Live-активность: последние операции

### Аккаунты

- Список с health score
- Детали: сессия, прокси, теги
- Действия: проверка, удаление, warmup

### Каналы

- Список с subscriber count
- Детали: username, about,admins
- Bulk: редактирование, публикация

### Операции

- Очередь: pending, running, done, failed
- Прогресс: live bar с ETA
- Детали: лог, ошибки, retry

---

## Ошибки

### Обработка ошибок API

```javascript
try {
  const data = await api('/api/miniapp/some-endpoint');
  // обработка
} catch(e) {
  toast('⚠️ ' + (e.message || 'Ошибка').substring(0, 60));
}
```

### SSE Reconnect

```javascript
SSE.onerror = () => {
  _sseRetry = Math.min(_sseRetry + 1, 5);
  setTimeout(openSSE, Math.min(6000 * Math.pow(2, _sseRetry - 1), 60000));
};
```

---

## Оптимизация

- Touch-friendly интерфейс
- Минимальный размер бандла
- Lazy loading для тяжёлых секций
- LocalStorage для кэширования
