# Nevesty Models — CRM для модельного агентства

> Полноценная платформа для модельного агентства: сайт-каталог, CRM, Telegram-бот, система 28+ AI-агентов для автоматического контроля качества.

---

## Обзор проекта

**Nevesty Models** — production-ready CRM для модельного агентства.

**Стек:** Node.js, Express, SQLite, Telegram Bot API (node-telegram-bot-api), vanilla JS + HTML/CSS

**Составные части:**

- **Сайт** — публичный каталог моделей, многошаговая форма бронирования, страница статуса заказа, личный кабинет клиента
- **Панель администратора** — управление заказами, моделями, клиентами, рассылками, аналитика, экспорт
- **Telegram-бот** — каталог, 4-шаговое бронирование, команды для администраторов, уведомления, OTP-авторизация
- **Telegram Mini App** — сайт открывается внутри Telegram с автозаполнением данных пользователя
- **AI Factory** — AI-агенты для автоматической генерации описаний, CEO Intelligence, оркестратор (28 агентов)
- **Система агентов** — 4 отряда (Reliability, Fix, Quality, Ops) запускаются автоматически после каждого изменения кода

---

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone <repo_url>
cd nevesty-models
```

### 2. Настроить окружение

```bash
cp .env.example .env
nano .env   # заполнить токены — см. раздел «Переменные окружения»
```

### 3. Запустить через deploy.sh (рекомендуется для продакшена)

```bash
./deploy.sh
```

Скрипт выполняет: `npm install`, инициализацию БД, заполнение тестовыми моделями, установку PM2, запуск сервисов.

### 4. Ручная установка (альтернатива)

```bash
npm install --production
mkdir -p logs
node database.js           # инициализация схемы SQLite (v1–v42)
node tools/seed-models.js  # заполнить тестовыми данными
pm2 start ecosystem.config.js
pm2 save
pm2 startup                # скопировать и выполнить напечатанную команду
```

### 5. Режим разработки

```bash
npm install
npm run dev    # запуск через nodemon с авто-перезагрузкой
```

Открыть в браузере: `http://localhost:3000`

---

## Структура проекта

```
nevesty-models/
├── server.js                   # Express-сервер (точка входа)
├── bot.js                      # Telegram-бот (бронирование, каталог, admin)
├── database.js                 # SQLite + миграции (v1–v42)
├── ecosystem.config.js         # PM2: конфигурация процессов
├── deploy.sh                   # Автоматический деплой для продакшена
├── docker-compose.yml          # Docker: app + redis + nginx + factory + certbot
├── Dockerfile                  # Образ Node.js приложения
├── routes/
│   ├── api.js                  # REST API (основной, ~10 000 строк)
│   ├── promo.js                # API промокодов
│   └── admin.js                # Дополнительные admin-маршруты
├── middleware/
│   └── auth.js                 # JWT middleware
├── handlers/
│   ├── index.js                # Обработчики бота (основные команды)
│   └── admin.js                # Admin-команды бота
├── services/
│   ├── scheduler.js            # Планировщик (напоминания, события)
│   ├── mailer.js               # Email-уведомления (SMTP / SendGrid)
│   ├── sms.js                  # SMS (SMS.ru / SMSC / Twilio)
│   ├── whatsapp.js             # WhatsApp Business API
│   ├── payment.js              # YooKassa / Stripe
│   ├── crm.js                  # AmoCRM / Bitrix24 интеграция
│   ├── instagram.js            # Instagram Graph API
│   ├── analytics-extra.js      # Расширенная аналитика
│   ├── cache.js                # In-memory / Redis кэш
│   ├── email.js                # Шаблоны писем
│   ├── logger.js               # Структурированное логирование
│   ├── sitemap.js              # Генерация sitemap.xml
│   └── payments.js             # Платёжный роутер
├── agents/                     # AI-агенты (28 модулей + оркестратор)
│   ├── run-organism.js         # Запуск всего «организма» агентов
│   ├── orchestrator.js         # Оркестратор агентов
│   ├── smart-orchestrator.js   # Smart-оркестратор с CEO Intelligence
│   ├── bug-hunter.js           # Поиск багов
│   ├── auto-fixer.js           # Автоматическое исправление
│   ├── scheduler.js            # Планировщик запусков агентов
│   ├── 01-ux-architect.js … 28-activity-logger.js  # 28 специализированных агентов
│   ├── departments/            # Отделы агентов
│   ├── fixers/                 # Агенты-фиксеры
│   └── lib/                   # Общие утилиты агентов
├── utils/
│   ├── documents.js            # Генерация договоров и счетов (HTML)
│   ├── constants.js            # Константы приложения
│   ├── helpers.js              # Общие вспомогательные функции
│   └── strings.js              # Строковые утилиты
├── locales/
│   ├── ru.json                 # Русский (основной)
│   ├── en.json                 # English
│   └── uk.json                 # Українська
├── keyboards/                  # Клавиатуры Telegram-бота
├── tools/
│   ├── notify.js               # CLI-нотификатор в Telegram
│   └── seed-models.js          # Заполнение тестовыми моделями
├── scripts/
│   └── backup.sh               # Скрипт резервного копирования БД
├── public/                     # Frontend (HTML/CSS/JS)
│   ├── index.html              # Главная страница
│   ├── catalog.html            # Каталог моделей
│   ├── booking.html            # Форма бронирования (4 шага)
│   ├── model.html              # Карточка модели
│   ├── order-status.html       # Статус заказа
│   ├── cabinet.html            # Личный кабинет клиента
│   ├── search.html             # Поиск
│   ├── favorites.html          # Избранное
│   ├── compare.html            # Сравнение моделей
│   ├── pricing.html            # Прайс-лист
│   ├── reviews.html            # Отзывы
│   ├── admin/                  # Панель администратора (20+ страниц)
│   ├── dashboard/              # Dashboard агентов (React + React Flow)
│   └── js/                     # Клиентские скрипты
├── tests/                      # Jest-тесты (100+ файлов, wave-серии)
├── nginx/                      # Конфиг nginx для продакшена
├── logs/                       # PM2-логи (создаётся автоматически)
├── uploads/                    # Фотографии моделей
├── backups/                    # Резервные копии БД
├── data.db                     # SQLite база данных
├── strings.js                  # Строки бота (i18n)
├── constants.js                # Глобальные константы
└── .env.example                # Шаблон переменных окружения
```

---

## Ключевые функции

1. Публичный каталог моделей с фильтрацией по параметрам (рост, возраст, город, тип)
2. Поиск моделей с полнотекстовым поиском
3. Сравнение моделей (до N карточек)
4. Избранное / вишлист
5. Многошаговая форма бронирования (4 шага) с валидацией
6. Быстрое бронирование (quick-booking) без полной формы
7. Статус заказа по номеру брони (без авторизации)
8. Отмена заказа клиентом
9. Личный кабинет клиента (история заказов, повторный заказ, профиль, баллы лояльности)
10. OTP-авторизация клиента через SMS / Email
11. Система лояльности — баллы за завершённые заказы
12. Telegram-бот: каталог, бронирование, статус заказа, FAQ
13. Telegram Mini App — сайт внутри Telegram с автозаполнением
14. Уведомления клиентам в Telegram / Email / SMS / WhatsApp
15. Панель администратора — управление заказами с историей изменений
16. Управление моделями: фото-галерея, занятость, архивирование, клонирование
17. Управление расписанием занятости моделей (calendar, busy-dates)
18. AI-генерация описаний для моделей (Anthropic Claude)
19. Рассылки клиентам через Telegram-бота (broadcast)
20. Промокоды со скидками и статистикой использования
21. Система тарифных пакетов (price packages)
22. Генерация договоров и счетов в HTML
23. Экспорт данных в CSV (заказы, клиенты, модели)
24. Аналитика: KPI, воронка продаж, выручка, повторные клиенты, LTV, прогноз
25. Интеграция с CRM: AmoCRM, Bitrix24
26. Интеграция платёжных систем: YooKassa, Stripe
27. Публичная страница статуса системы (`/status`) с историей аптайма
28. 28 AI-агентов для автоматического аудита и улучшения кода
29. In-memory / Redis кэш с авто-переключением
30. Поддержка трёх языков: ru / en / uk

---

## API Endpoints

Все endpoints доступны по префиксу `/api/`. Защищённые маршруты требуют заголовок `Authorization: Bearer <JWT>`.

### Публичные

| Метод | Путь                                       | Описание                              |
| ----- | ------------------------------------------ | ------------------------------------- |
| GET   | `/api/config`                              | Публичная конфигурация приложения     |
| GET   | `/api/cities`                              | Список доступных городов              |
| GET   | `/api/models`                              | Список активных моделей (с фильтрами) |
| GET   | `/api/models/search`                       | Полнотекстовый поиск моделей          |
| GET   | `/api/models/related`                      | Похожие модели                        |
| GET   | `/api/models/:id`                          | Карточка модели                       |
| GET   | `/api/models/:id/photos`                   | Фото-галерея модели                   |
| GET   | `/api/models/:id/availability`             | Доступность модели                    |
| POST  | `/api/models/:id/view`                     | Трекинг просмотра                     |
| POST  | `/api/orders`                              | Создать заказ (бронирование)          |
| GET   | `/api/orders/status/:order_number`         | Статус заказа                         |
| GET   | `/api/orders/status/:order_number/history` | История статусов                      |
| PATCH | `/api/orders/status/:order_number/cancel`  | Отмена заказа клиентом                |
| GET   | `/api/orders/by-phone`                     | Заказы по номеру телефона             |
| POST  | `/api/quick-booking`                       | Быстрое бронирование                  |
| POST  | `/api/contact`                             | Форма обратной связи                  |
| POST  | `/api/promo/check`                         | Проверка промокода                    |
| GET   | `/api/faq`                                 | FAQ список                            |
| GET   | `/api/faq/categories`                      | Категории FAQ                         |
| POST  | `/api/chat/ask`                            | AI-чат (Claude)                       |
| GET   | `/api/recommend`                           | Рекомендации моделей                  |
| GET   | `/api/budget-estimate`                     | Оценка бюджета                        |
| GET   | `/api/status`                              | Статус системы (uptime)               |
| GET   | `/api/status/history`                      | История uptime                        |

### Клиент (OTP-авторизация)

| Метод | Путь                       | Описание           |
| ----- | -------------------------- | ------------------ |
| POST  | `/api/client/request-code` | Запросить OTP-код  |
| POST  | `/api/client/verify`       | Верифицировать OTP |
| GET   | `/api/client/orders`       | Заказы клиента     |
| POST  | `/api/client/review`       | Оставить отзыв     |
| POST  | `/api/client/ai-match`     | AI-подбор модели   |
| POST  | `/api/client/ai-budget`    | AI-расчёт бюджета  |

### Личный кабинет клиента

| Метод | Путь                             | Описание         |
| ----- | -------------------------------- | ---------------- |
| POST  | `/api/cabinet/login`             | Вход в кабинет   |
| GET   | `/api/cabinet/orders`            | История заказов  |
| GET   | `/api/cabinet/profile`           | Профиль          |
| PATCH | `/api/cabinet/profile`           | Обновить профиль |
| POST  | `/api/cabinet/orders/:id/repeat` | Повторить заказ  |
| GET   | `/api/cabinet/loyalty`           | Баллы лояльности |
| GET   | `/api/cabinet/reviews`           | Мои отзывы       |

### Аутентификация (Admin)

| Метод | Путь                    | Описание               |
| ----- | ----------------------- | ---------------------- |
| POST  | `/api/admin/login`      | Вход (возвращает JWT)  |
| POST  | `/api/auth/verify-totp` | Верификация 2FA        |
| POST  | `/api/auth/refresh`     | Обновить токен         |
| POST  | `/api/auth/logout`      | Выход                  |
| GET   | `/api/admin/me`         | Профиль администратора |

### Администратор — модели

| Метод  | Путь                                         | Описание                     |
| ------ | -------------------------------------------- | ---------------------------- |
| GET    | `/api/admin/models`                          | Список моделей (с фильтрами) |
| POST   | `/api/admin/models/json`                     | Создать модель               |
| PUT    | `/api/admin/models/:id/json`                 | Полное обновление модели     |
| PATCH  | `/api/admin/models/:id`                      | Частичное обновление         |
| DELETE | `/api/admin/models/:id`                      | Удалить модель               |
| POST   | `/api/admin/models/:id/photos`               | Загрузить фото               |
| DELETE | `/api/admin/models/:id/photos/:photoId`      | Удалить фото                 |
| PATCH  | `/api/admin/models/:id/archive`              | Архивировать                 |
| POST   | `/api/admin/models/:id/duplicate`            | Клонировать                  |
| POST   | `/api/admin/models/:id/generate-description` | AI-описание                  |
| GET    | `/api/admin/models/:id/availability`         | Доступность                  |
| POST   | `/api/admin/models/:id/busy-dates`           | Добавить занятые даты        |

### Администратор — заказы

| Метод | Путь                                  | Описание                    |
| ----- | ------------------------------------- | --------------------------- |
| GET   | `/api/admin/orders`                   | Список заказов              |
| GET   | `/api/admin/orders/:id`               | Детали заказа               |
| PUT   | `/api/admin/orders/:id`               | Обновить заказ              |
| POST  | `/api/admin/orders/:id/message`       | Отправить сообщение клиенту |
| POST  | `/api/admin/orders/:id/pay`           | Создать оплату              |
| POST  | `/api/admin/orders/bulk-status`       | Массовое изменение статуса  |
| GET   | `/api/admin/orders/export`            | Экспорт в CSV               |
| GET   | `/api/admin/orders/:id/contract.html` | Договор                     |
| GET   | `/api/admin/orders/:id/invoice.html`  | Счёт                        |
| GET   | `/api/admin/orders/:id/calendar.ics`  | iCal файл                   |

### Администратор — аналитика

| Метод | Путь                                  | Описание           |
| ----- | ------------------------------------- | ------------------ |
| GET   | `/api/admin/stats`                    | Сводная статистика |
| GET   | `/api/admin/analytics/kpi`            | KPI                |
| GET   | `/api/admin/analytics/funnel`         | Воронка продаж     |
| GET   | `/api/admin/analytics/top-models`     | Топ моделей        |
| GET   | `/api/admin/analytics/revenue`        | Выручка            |
| GET   | `/api/admin/analytics/forecast`       | Прогноз            |
| GET   | `/api/admin/analytics/client-ltv`     | LTV клиентов       |
| GET   | `/api/admin/analytics/repeat-clients` | Повторные клиенты  |

### Администратор — прочее

| Метод | Путь                                | Описание                     |
| ----- | ----------------------------------- | ---------------------------- |
| GET   | `/api/admin/clients`                | Список клиентов              |
| POST  | `/api/admin/broadcasts`             | Создать рассылку             |
| GET   | `/api/admin/promo`                  | Промокоды                    |
| POST  | `/api/admin/promo`                  | Создать промокод             |
| GET   | `/api/admin/faq`                    | FAQ (admin)                  |
| POST  | `/api/admin/factory/cycle-complete` | Вебхук завершения AI Factory |
| POST  | `/api/admin/crm/sync/:provider`     | Синхронизация с CRM          |
| GET   | `/api/admin/system`                 | Системная информация         |
| GET   | `/api/admin/db-stats`               | Статистика БД                |
| POST  | `/api/admin/db-backup`              | Создать резервную копию      |
| GET   | `/api/admin/export/orders.csv`      | CSV заказов                  |
| GET   | `/api/admin/export/clients.csv`     | CSV клиентов                 |
| GET   | `/api/admin/export/models.csv`      | CSV моделей                  |

### Вебхуки

| Метод | Путь                          | Описание              |
| ----- | ----------------------------- | --------------------- |
| POST  | `/api/webhooks/yookassa`      | YooKassa (платёж)     |
| POST  | `/api/webhooks/stripe`        | Stripe (платёж)       |
| POST  | `/api/webhooks/crm/:provider` | Входящие события CRM  |
| POST  | `/api/webhooks/order`         | Внешний вебхук заказа |
| POST  | `/api/webhooks/review`        | Внешний вебхук отзыва |

---

## Переменные окружения

Скопируйте `.env.example` в `.env` и заполните значения.

### Обязательные

| Переменная           | Описание                                                 |
| -------------------- | -------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather                                 |
| `ADMIN_TELEGRAM_IDS` | Telegram ID администраторов (через запятую)              |
| `JWT_SECRET`         | Секрет JWT — минимум 32 символа (`openssl rand -hex 32`) |
| `ADMIN_PASSWORD`     | Пароль для панели администратора                         |

### Сервер

| Переменная    | По умолчанию | Описание                                    |
| ------------- | ------------ | ------------------------------------------- |
| `PORT`        | `3000`       | Порт Express-сервера                        |
| `NODE_ENV`    | `production` | Окружение                                   |
| `SITE_URL`    | —            | Полный URL сайта (для бота, sitemap, OG)    |
| `WEBHOOK_URL` | —            | URL вебхука Telegram (пусто = long polling) |
| `DB_PATH`     | `./data.db`  | Путь к файлу SQLite                         |

### Безопасность

| Переменная             | Описание                                    |
| ---------------------- | ------------------------------------------- |
| `JWT_EXPIRES_IN`       | Срок жизни JWT (по умолчанию `7d`)          |
| `REFRESH_TOKEN_SECRET` | Секрет refresh-токена                       |
| `SESSION_SECRET`       | Секрет сессии                               |
| `ADMIN_USERNAME`       | Логин администратора (по умолчанию `admin`) |
| `TOTP_ENABLED`         | Включить 2FA для admin (`true`/`false`)     |

### Email / SMS / WhatsApp

| Переменная                                 | Описание                                   |
| ------------------------------------------ | ------------------------------------------ |
| `SMTP_HOST`                                | SMTP-сервер (оставить пустым для DEV_MODE) |
| `SMTP_USER` / `SMTP_PASS`                  | SMTP-учётные данные                        |
| `SENDGRID_API_KEY`                         | Альтернатива SMTP (приоритет над SMTP)     |
| `SMS_PROVIDER`                             | `smsru` / `smsc` / `twilio` / пусто        |
| `SMS_RU_API_KEY`                           | API-ключ SMS.ru                            |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio                                     |
| `WHATSAPP_TOKEN`                           | WhatsApp Business Cloud API                |

### Платежи

| Переменная                                    | Описание |
| --------------------------------------------- | -------- |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY`    | YooKassa |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe   |

### AI / Агенты

| Переменная            | По умолчанию        | Описание                              |
| --------------------- | ------------------- | ------------------------------------- |
| `ANTHROPIC_API_KEY`   | —                   | API-ключ Claude (для AI-функций)      |
| `FACTORY_CYCLE_HOURS` | `6`                 | Интервал запуска AI Factory (в часах) |
| `FACTORY_MODEL`       | `claude-sonnet-4-6` | Модель Claude для Factory             |

### CRM / Аналитика

| Переменная                                 | Описание                  |
| ------------------------------------------ | ------------------------- |
| `AMOCRM_SUBDOMAIN` / `AMOCRM_ACCESS_TOKEN` | AmoCRM OAuth2             |
| `BITRIX24_WEBHOOK_URL`                     | Исходящий вебхук Bitrix24 |
| `GA4_MEASUREMENT_ID`                       | Google Analytics 4        |
| `YANDEX_METRICA_ID`                        | Яндекс.Метрика            |

### Кэш / Redis

| Переменная  | Описание                          |
| ----------- | --------------------------------- |
| `REDIS_URL` | URL Redis (пусто = in-memory кэш) |

### Лимиты и мониторинг

| Переменная             | По умолчанию | Описание                        |
| ---------------------- | ------------ | ------------------------------- |
| `RATE_LIMIT_WINDOW_MS` | `900000`     | Окно rate limit (мс)            |
| `RATE_LIMIT_MAX`       | `100`        | Макс. запросов в окне           |
| `MEMORY_ALERT_MB`      | `500`        | Порог алерта по памяти          |
| `MAX_FILE_SIZE_MB`     | `10`         | Макс. размер загружаемого файла |
| `LOG_LEVEL`            | `info`       | Уровень логирования             |

---

## Тесты

```bash
npm test                  # Все тесты (Jest, forceExit)
npm run test:e2e          # Только E2E-тесты
npm run test:coverage     # Тесты с отчётом о покрытии
npm run test:watch        # Тесты в режиме наблюдения
```

Тесты находятся в `/tests/`. Более 100 файлов: `api-wave*.test.js`, E2E-тесты (`e2e-*.test.js`), юнит-тесты (`unit-*.test.js`).

```bash
# Запустить конкретный тест
npx jest tests/e2e-booking-flow.test.js

# Запустить группу
npx jest tests/api-wave1
```

---

## База данных

Файл: `data.db` (создаётся автоматически при первом запуске через `node database.js`)

Схема версионирована: текущая версия **v42**.

| Таблица                       | Описание                        |
| ----------------------------- | ------------------------------- |
| `models`                      | Модели агентства                |
| `orders`                      | Заказы / бронирования           |
| `telegram_sessions`           | Состояние сессий бота           |
| `agent_logs`                  | История запусков AI-агентов     |
| `promo_codes`                 | Промокоды и скидки              |
| `model_photos`                | Фото-галерея моделей            |
| `model_availability_schedule` | Расписание занятости            |
| `reviews`                     | Отзывы клиентов                 |
| `wishlists`                   | Избранные модели                |
| `broadcasts`                  | Telegram-рассылки               |
| `message_templates`           | Шаблоны сообщений               |
| `support_messages`            | Чат поддержки клиент ↔ менеджер |
| `faq`                         | FAQ                             |
| `social_posts`                | Посты для соцсетей              |
| `webhook_logs`                | Логи входящих вебхуков          |
| `uptime_logs`                 | История аптайма системы         |
| `error_logs`                  | Логи необработанных исключений  |
| `refresh_tokens`              | Refresh-токены                  |
| `client_prefs`                | Настройки уведомлений клиентов  |

---

## Деплой

### Через Docker Compose (рекомендуется)

```bash
# Запустить все сервисы (app + redis + nginx + factory)
docker-compose up -d

# Просмотр логов
docker-compose logs -f app
docker-compose logs -f nginx

# Остановить
docker-compose down

# Пересобрать образы
docker-compose build
```

Docker Compose поднимает:

- `app` — Node.js приложение (порт 3000 внутри сети)
- `redis` — Redis 7 (кэш)
- `factory` — AI Factory (агенты)
- `nginx` — Reverse proxy (порты 80/443)
- `certbot` — авто-обновление SSL (профиль `ssl`)

### Через PM2 (bare-metal / VPS)

```bash
./deploy.sh                         # Первоначальный деплой

pm2 reload ecosystem.config.js      # Zero-downtime перезапуск
pm2 logs                            # Логи всех процессов
pm2 monit                           # CPU/memory dashboard
```

### Cloud Deploy (Railway / Render / Heroku)

Переменные окружения задаются через Dashboard провайдера. Файлы `railway.json` и `render.yaml` уже настроены в репозитории.

### HTTPS / Telegram Mini App

Для работы Telegram Mini App требуется HTTPS:

```bash
# Получить SSL-сертификат (certbot внутри Docker Compose)
docker-compose --profile ssl up certbot

# Или использовать Cloudflare Tunnel (без nginx)
```

После настройки HTTPS: `SITE_URL=https://yourdomain.com` в `.env`.

---

## Панель администратора

- URL: `http://localhost:3000/admin/login.html`
- Логин: значение `ADMIN_USERNAME` из `.env` (по умолчанию `admin`)
- Пароль: значение `ADMIN_PASSWORD` из `.env`
- Обязательно смените пароль перед переходом в продакшен

---

## Система AI-агентов

28 специализированных агентов непрерывно мониторят и улучшают кодовую базу.

| Отряд                 | Триггер                      | Агенты                                                                     |
| --------------------- | ---------------------------- | -------------------------------------------------------------------------- |
| **Reliability Squad** | После каждого изменения кода | Security Auditor, Backend Reliability, Bot Integration, Frontend QA        |
| **Fix Squad**         | При обнаружении проблем      | Fix-Backend, Fix-Frontend, Fix-Bot, Fix-Infra                              |
| **Quality Squad**     | Раз в сессию                 | Code Reviewer, Accessibility Auditor, SEO Specialist, Performance Engineer |
| **Ops Squad**         | Перед деплоем                | DevOps Engineer, Monitoring Engineer, DB Architect, Test Engineer          |

```bash
# Запустить всех агентов вручную
node agents/run-organism.js

# По расписанию (каждые 30 минут)
pm2 start agents/run-organism.js --name organism --cron "*/30 * * * *" --no-autorestart
pm2 save
```

### Telegram-нотификатор

```bash
node tools/notify.js --from "Agent: DevOps" "✅ Деплой завершён"
```

---

## Статусы заказов

| Статус        | Описание          |
| ------------- | ----------------- |
| `new`         | Только что создан |
| `in_review`   | На рассмотрении   |
| `confirmed`   | Подтверждён       |
| `in_progress` | В работе          |
| `completed`   | Завершён          |
| `rejected`    | Отклонён          |

---

## Требования

| Зависимость | Версия                                    |
| ----------- | ----------------------------------------- |
| Node.js     | 18+                                       |
| npm         | 8+                                        |
| SQLite      | поставляется через `sqlite3` npm          |
| PM2         | любая (устанавливается через `deploy.sh`) |
| Redis       | 7+ (опционально, через Docker)            |
| ОС          | Linux / macOS (Windows через WSL)         |
