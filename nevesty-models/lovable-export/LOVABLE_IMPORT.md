# Nevesty Models → Lovable: Руководство по интеграции

## ⚠️ Важно: этот export НЕ заменяет ваш проект

Папка `lovable-export/` содержит **дополнения** к уже существующему Lovable-проекту.
Ваш проект уже имеет: catalog, quiz, bookings, admin, wallets, Telegram webhook.

Здесь находятся **недостающие части**.

---

## Что добавляет этот export

| Файл                                           | Добавляет                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `supabase/migrations/002_missing_features.sql` | Таблицы: reviews, promo_codes, loyalty, referrals, broadcasts, experiments, faq, price_packages |
| `supabase/functions/telegram-webhook/`         | Telegram бот: вебхук, команды, уведомления о бронированиях                                      |
| `supabase/functions/send-sms/`                 | SMS через SMS.ru / SMSC / Twilio                                                                |
| `supabase/functions/send-email/`               | Email через SendGrid + HTML шаблоны                                                             |
| `supabase/functions/payment-webhook/`          | Вебхуки YooKassa и Stripe (авто-подтверждение заявок)                                           |
| `supabase/functions/broadcast/`                | Telegram рассылка клиентам                                                                      |
| `src/components/promo/PromoCodeInput`          | Поле ввода промокода с валидацией в реальном времени                                            |
| `src/components/reviews/ReviewsList`           | Список отзывов с рейтингом и ответами                                                           |
| `src/components/analytics/AnalyticsDashboard`  | Аналитика: заявки, выручка, конверсия, топ моделей                                              |
| `src/components/payments/PaymentButton`        | Кнопка оплаты (YooKassa/Stripe)                                                                 |
| `src/hooks/`                                   | useModels, useOrders, useSettings, useReviews, useAuth                                          |
| `src/types/index.ts`                           | TypeScript типы для всех 35+ таблиц                                                             |

---

## Шаг 1: Применить миграцию БД

В Supabase Dashboard → **SQL Editor**:

```sql
-- Вставь содержимое файла:
-- lovable-export/supabase/migrations/002_missing_features.sql
```

> Это добавит только НОВЫЕ таблицы. Существующие `models`, `bookings`, `app_settings` не тронет.

---

## Шаг 2: Задеплоить Edge Functions

```bash
# Установи Supabase CLI если ещё нет:
npm install -g supabase

# Войди в проект:
supabase link --project-ref <YOUR_PROJECT_REF>

# Задеплой функции:
supabase functions deploy telegram-webhook
supabase functions deploy send-sms
supabase functions deploy send-email
supabase functions deploy payment-webhook
supabase functions deploy broadcast
```

---

## Шаг 3: Задать секреты для Edge Functions

В Supabase Dashboard → **Edge Functions** → **Manage secrets**:

```bash
# Или через CLI:
supabase secrets set TELEGRAM_BOT_TOKEN=your_token
supabase secrets set TELEGRAM_WEBHOOK_SECRET=your_secret
supabase secrets set SENDGRID_API_KEY=SG.xxxx
supabase secrets set SMS_PROVIDER=smsru
supabase secrets set SMS_RU_API_KEY=your_key
supabase secrets set YOOKASSA_SECRET_KEY=your_key
supabase secrets set STRIPE_WEBHOOK_SECRET=whsec_xxx
```

---

## Шаг 4: Подключить Telegram Webhook

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<PROJECT_REF>.supabase.co/functions/v1/telegram-webhook" \
  -d "secret_token=<WEBHOOK_SECRET>"
```

---

## Шаг 5: Скопировать компоненты в проект

```
lovable-export/src/
  types/index.ts              → src/types/index.ts
  lib/supabase.ts             → src/lib/supabase.ts (если ещё нет)
  hooks/                      → src/hooks/
  components/
    promo/PromoCodeInput.tsx  → добавь в форму бронирования
    reviews/ReviewsList.tsx   → добавь на страницу модели / каталог
    analytics/AnalyticsDashboard.tsx → добавь в /admin/analytics
    payments/PaymentButton.tsx → добавь в карточку заявки
```

---

## Шаг 6: Добавить промокод в форму бронирования

В вашем существующем компоненте формы бронирования:

```tsx
import { PromoCodeInput } from '../components/promo';

// В форме:
<PromoCodeInput
  budget={parseFloat(form.budget || '0')}
  onApply={({ code, discount_type, discount_value }) => {
    setForm(f => ({
      ...f,
      promo_code: code,
      promo_discount: discount_type === 'percent' ? (parseFloat(f.budget) * discount_value) / 100 : discount_value,
    }));
  }}
/>;
```

---

## Шаг 7: Добавить отзывы на страницу модели

```tsx
import { ReviewsList } from '../components/reviews';

// На странице модели:
<ReviewsList modelId={model.id} limit={5} />;
```

---

## Шаг 8: Добавить аналитику в админку

```tsx
import { AnalyticsDashboard } from '../components/analytics';

// В /admin/analytics:
<AnalyticsDashboard />;
```

---

## Что NOT входит (остаётся на Node.js сервере)

| Компонент                       | Почему                        | Где запускать   |
| ------------------------------- | ----------------------------- | --------------- |
| AI Factory (Python, 49 агентов) | Требует Python + Claude API   | VPS/Railway     |
| Полный Telegram бот (`bot.js`)  | 735KB, сложная логика         | VPS/Railway     |
| SQLite → PostgreSQL синк        | Только если мигрируете данные | One-time script |

### Рекомендуемая архитектура:

```
Lovable (React + Supabase PostgreSQL)
  ├── Публичный каталог
  ├── Форма бронирования
  ├── Панель администратора
  └── Edge Functions (webhook, SMS, email, payments)

VPS / Railway
  ├── AI Factory (Python, 49 агентов, CEO Intelligence)
  └── Синхронизация с Supabase через PostgreSQL connection string
```

---

## Таблицы: что уже есть vs что добавляется

| Таблица                                    | Статус                                |
| ------------------------------------------ | ------------------------------------- |
| `models`, `bookings`, `order_messages`     | ✅ Уже в вашем проекте                |
| `managers`, `app_settings`, `bot_sessions` | ✅ Уже в вашем проекте                |
| `client_wallets`, `wallet_transactions`    | ✅ Уже в вашем проекте                |
| `contact_unlocks`, `user_roles`            | ✅ Уже в вашем проекте                |
| `reviews`                                  | ➕ Добавляет 002_missing_features.sql |
| `promo_codes`                              | ➕ Добавляет 002_missing_features.sql |
| `loyalty_points`, `loyalty_transactions`   | ➕ Добавляет 002_missing_features.sql |
| `referrals`                                | ➕ Добавляет 002_missing_features.sql |
| `scheduled_broadcasts`                     | ➕ Добавляет 002_missing_features.sql |
| `ab_experiments`                           | ➕ Добавляет 002_missing_features.sql |
| `faq`                                      | ➕ Добавляет 002_missing_features.sql |
| `price_packages`                           | ➕ Добавляет 002_missing_features.sql |

---

## Переменные окружения (в Lovable)

```env
VITE_SUPABASE_URL=https://xxxx.supabase.co
VITE_SUPABASE_ANON_KEY=eyJhbGci...
```

Lovable подставляет их автоматически.
