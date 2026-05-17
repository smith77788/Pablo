# Nevesty Models — Импорт в Lovable

## Что это такое

Папка `lovable-export/` содержит всё необходимое для переноса Nevesty Models на платформу [Lovable](https://lovable.dev) (TanStack Start + React + Supabase).

## Шаг 1: Создай проект в Lovable

1. Открой [lovable.dev](https://lovable.dev) → **New Project**
2. Выбери шаблон **TanStack Start + Supabase**
3. Создай проект

## Шаг 2: Подключи Supabase

1. В Lovable → **Settings** → **Integrations** → **Supabase**
2. Создай новый Supabase проект (или выбери существующий)
3. Lovable автоматически подставит `VITE_SUPABASE_URL` и `VITE_SUPABASE_ANON_KEY`

## Шаг 3: Применить миграции БД

В Supabase Dashboard → **SQL Editor** выполни:

```sql
-- Вставь содержимое файла:
-- lovable-export/supabase/migrations/001_initial_schema.sql
```

Или через Supabase CLI:

```bash
supabase db push
```

## Шаг 4: Скопировать файлы в проект Lovable

Скопируй из `lovable-export/` в твой Lovable проект:

```
lovable-export/
  src/
    types/index.ts         → src/types/index.ts
    lib/supabase.ts        → src/lib/supabase.ts
    hooks/                 → src/hooks/
      useModels.ts
      useOrders.ts
      useSettings.ts
      useReviews.ts
      useAuth.ts
      index.ts
    components/
      catalog/             → src/components/catalog/
        ModelCard.tsx
        CatalogPage.tsx
        index.ts
      booking/             → src/components/booking/
        BookingForm.tsx
        index.ts
      admin/               → src/components/admin/
        AdminDashboard.tsx
        SettingsPanel.tsx
        index.ts
```

## Шаг 5: Установить зависимости

В терминале Lovable:

```bash
npm install @supabase/supabase-js @tanstack/react-query
```

## Шаг 6: Настроить QueryClient

В `src/main.tsx` или `src/app.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 1000 * 60 * 5 }, // 5 минут кэш
  },
});

// Оберни приложение:
<QueryClientProvider client={queryClient}>
  <App />
</QueryClientProvider>;
```

## Шаг 7: Добавить маршруты

В TanStack Start используй TanStack Router:

```tsx
// routes/index.tsx — Главная / каталог
import { CatalogPage } from '../components/catalog';

// routes/model.$id.tsx — Страница модели
// routes/booking.tsx — Бронирование
// routes/admin/index.tsx — Панель администратора (защищённый маршрут)
// routes/admin/settings.tsx — Настройки
```

## Шаг 8: Настроить RLS (безопасность)

В Supabase Dashboard → **Authentication** → **Policies**:

- Таблица `models`: публичное чтение (не архивированные)
- Таблица `orders`: только авторизованные (admins)
- Таблица `reviews`: публичное чтение (approved), write — все
- Таблица `bot_settings`: только авторизованные (admins)

Политики уже созданы миграцией в шаге 3.

## Шаг 9: Настроить аутентификацию

В Supabase → **Authentication** → **Providers**:

- Включи **Email** провайдер
- Создай первого администратора через **Users** → **Invite user**

## Структура таблиц БД

| Таблица          | Описание                 |
| ---------------- | ------------------------ |
| `models`         | Профили моделей          |
| `orders`         | Заявки на бронирование   |
| `reviews`        | Отзывы клиентов          |
| `bot_settings`   | Настройки (key-value)    |
| `admins`         | Администраторы           |
| `agent_logs`     | Логи AI-агентов          |
| `ab_experiments` | A/B эксперименты         |
| `loyalty_points` | Баллы лояльности         |
| `wishlists`      | Избранные модели         |
| `faq`            | Часто задаваемые вопросы |
| `price_packages` | Пакеты услуг             |
| `promo_codes`    | Промокоды                |
| ...              | +30 таблиц               |

Полный список: `lovable-export/supabase/migrations/001_initial_schema.sql`

## Переменные окружения

```env
VITE_SUPABASE_URL=https://xxxx.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbGci...
```

Lovable подставляет их автоматически при подключении Supabase.

## Что НЕ переносится на Lovable

| Компонент                | Причина                       | Альтернатива            |
| ------------------------ | ----------------------------- | ----------------------- |
| Telegram бот (`bot.js`)  | Требует серверный процесс     | Оставь на VPS/Railway   |
| API сервер (`server.js`) | Express не работает в Lovable | Supabase Edge Functions |
| Файлы `uploads/`         | Локальная файловая система    | Supabase Storage        |
| SQLite миграции          | PostgreSQL вместо SQLite      | ✅ Уже конвертированы   |
| AI Factory (Python)      | Требует серверный процесс     | Оставь на VPS/Railway   |

## Рекомендуемая архитектура после переноса

```
Lovable (Frontend + Supabase)
  ├── Публичный сайт (каталог, бронирование)
  ├── Панель администратора
  └── Supabase PostgreSQL (БД)

VPS / Railway (Backend)
  ├── Telegram бот (bot.js)
  ├── AI Factory (Python)
  └── Обращается к той же Supabase БД
```

Бот и Factory можно настроить работать с Supabase через PostgreSQL connection string вместо SQLite.
