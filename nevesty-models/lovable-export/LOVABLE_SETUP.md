# Nevesty Models — Lovable Setup Guide

## Что уже сделано ✅

Все файлы для переноса находятся в репозитории `smith77788/Pablo` (ветка `claude/modeling-agency-website-jp2Qd`), папка `nevesty-models/lovable-export/`:

| Файл                                           | Описание                                                                      |
| ---------------------------------------------- | ----------------------------------------------------------------------------- |
| `supabase/migrations/002_missing_features.sql` | Добавляет недостающие таблицы (reviews, promo_codes, loyalty, referrals, ...) |
| `supabase/functions/telegram-webhook/`         | Edge Function — Telegram webhook                                              |
| `supabase/functions/send-sms/`                 | Edge Function — SMS уведомления                                               |
| `supabase/functions/send-email/`               | Edge Function — Email уведомления                                             |
| `supabase/functions/payment-webhook/`          | Edge Function — YooKassa/Stripe                                               |
| `supabase/functions/broadcast/`                | Edge Function — рассылка                                                      |
| `src/components/catalog/`                      | Каталог моделей + карточка                                                    |
| `src/components/booking/`                      | 4-шаговая форма бронирования                                                  |
| `src/components/admin/`                        | Дашборд + панель настроек                                                     |
| `src/components/analytics/`                    | Аналитика                                                                     |
| `src/components/reviews/`                      | Отзывы                                                                        |
| `src/components/promo/`                        | Промокоды                                                                     |
| `src/components/payments/`                     | Кнопка оплаты (YooKassa/Stripe)                                               |
| `src/hooks/`                                   | React hooks для Supabase                                                      |
| `src/types/index.ts`                           | TypeScript типы для всех 35 таблиц                                            |
| `src/lib/supabase.ts`                          | Supabase client                                                               |

---

## Способ 1: Авто-синхронизация (скрипт)

Запусти на своём компьютере:

```bash
# Скачай скрипт
curl -O https://raw.githubusercontent.com/smith77788/Pablo/claude%2Fmodeling-agency-website-jp2Qd/nevesty-models/lovable-export/sync-to-lovable.sh
chmod +x sync-to-lovable.sh
./sync-to-lovable.sh
```

Скрипт:

1. Клонирует Pablo repo (нашу ветку)
2. Клонирует velvet-house-concierge (Lovable проект)
3. Копирует все файлы
4. Делает коммит и пушит в velvet-house

---

## Способ 2: Вручную через GitHub UI

1. Открой https://github.com/smith77788/Pablo/tree/claude/modeling-agency-website-jp2Qd/nevesty-models/lovable-export
2. Для каждого файла: открой файл → Raw → скопируй → создай в velvet-house с тем же путём

---

## Способ 3: Lovable Chat (рекомендуется для UI)

1. Открой свой Lovable проект на lovable.dev
2. Нажми "Chat"
3. Напиши: `@LOVABLE_PROMPT.md` или скопируй содержимое файла `LOVABLE_PROMPT.md`
4. Lovable сам сгенерирует весь React код и запушит в velvet-house

**LOVABLE_PROMPT.md уже в репозитории** (корень) — можно дать ссылку Lovable.

---

## После копирования файлов — настройка Supabase

### 1. Запустить миграцию

В Supabase Studio → SQL Editor → вставь и запусти:

```
nevesty-models/lovable-export/supabase/migrations/002_missing_features.sql
```

Это добавит недостающие таблицы: `reviews`, `promo_codes`, `loyalty_points`, `loyalty_transactions`, `referrals`, `scheduled_broadcasts`, `ab_experiments`, `notifications`, `faq`, `price_packages`.

### 2. Развернуть Edge Functions

```bash
# Установить Supabase CLI
npm install -g supabase

# В папке velvet-house
supabase functions deploy telegram-webhook --project-ref YOUR_PROJECT_REF
supabase functions deploy send-sms --project-ref YOUR_PROJECT_REF
supabase functions deploy send-email --project-ref YOUR_PROJECT_REF
supabase functions deploy payment-webhook --project-ref YOUR_PROJECT_REF
supabase functions deploy broadcast --project-ref YOUR_PROJECT_REF
```

### 3. Задать секреты Edge Functions

```bash
supabase secrets set TELEGRAM_BOT_TOKEN=your_bot_token
supabase secrets set TELEGRAM_ADMIN_IDS=123456789
supabase secrets set YOOKASSA_SHOP_ID=your_shop_id
supabase secrets set YOOKASSA_SECRET_KEY=your_secret
supabase secrets set STRIPE_WEBHOOK_SECRET=whsec_...
supabase secrets set SENDGRID_API_KEY=SG.xxx
```

### 4. Подключить Telegram webhook

```bash
curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook \
  -H "Content-Type: application/json" \
  -d '{"url": "https://YOUR_PROJECT.supabase.co/functions/v1/telegram-webhook"}'
```

---

## Переменные окружения для Lovable (`.env`)

```env
VITE_SUPABASE_URL=https://YOUR_PROJECT.supabase.co
VITE_SUPABASE_ANON_KEY=your_anon_key
VITE_YOOKASSA_SHOP_ID=your_shop_id
VITE_STRIPE_PUBLISHABLE_KEY=pk_live_...
```

---

## Структура готового приложения

```
velvet-house-concierge-9fba01e9/
├── src/
│   ├── components/
│   │   ├── catalog/       # Каталог моделей
│   │   ├── booking/       # Форма бронирования
│   │   ├── admin/         # Панель администратора
│   │   ├── analytics/     # Аналитика
│   │   ├── reviews/       # Отзывы
│   │   ├── promo/         # Промокоды
│   │   └── payments/      # Оплата
│   ├── hooks/             # React hooks
│   ├── types/             # TypeScript типы
│   └── lib/               # Supabase client
├── supabase/
│   ├── migrations/        # SQL миграции
│   └── functions/         # Edge Functions
└── LOVABLE_PROMPT.md      # Промпт для Lovable Chat
```
