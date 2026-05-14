# Nevesty Models — Telegram Bot + Modeling Agency Website

> Полнофункциональный сайт агентства моделей + Telegram-бот + система из 25 ИИ-агентов для непрерывного улучшения.

---

## Стек технологий

| Компонент | Технология |
|-----------|------------|
| Backend   | Node.js 18+ + Express |
| Database  | SQLite3 (better-sqlite3 compatible) |
| Telegram Bot | node-telegram-bot-api (polling) |
| Auth | JWT + bcryptjs |
| Process Manager | PM2 |
| Frontend | Vanilla HTML/CSS/JS |
| Agent Dashboard | React + React Flow |

---

## Структура проекта

```
nevesty-models/
├── bot.js              # Telegram-бот (4-шаговое бронирование, каталог, админ)
├── server.js           # Express HTTP сервер
├── database.js         # SQLite инициализация и helpers
├── routes/
│   └── api.js          # REST API (модели, заказы, авторизация)
├── middleware/
│   └── auth.js         # JWT middleware
├── public/
│   ├── index.html      # Главная страница
│   ├── catalog.html    # Каталог моделей
│   ├── booking.html    # Форма бронирования (4 шага)
│   ├── admin/          # Админ-панель
│   ├── dashboard/      # Дашборд агентов (React)
│   └── js/
│       ├── booking.js  # Логика формы бронирования
│       └── telegram-webapp.js  # Telegram Mini App интеграция
├── agents/
│   ├── lib/base.js     # Базовый класс Agent
│   ├── 01-ux-architect.js      # UX проверки меню и навигации
│   ├── 02-booking-completeness.js  # Полнота полей бронирования
│   ├── 03-model-showcase.js    # Витрина моделей
│   ├── 04-order-lifecycle.js   # Жизненный цикл заказа
│   ├── 05-client-experience.js # Клиентский опыт
│   ├── 06-admin-experience.js  # Опыт администратора
│   ├── 07-message-threading.js # Переписка админ↔клиент
│   ├── 08-notification-engine.js  # Уведомления
│   ├── 09-security-guard.js    # Безопасность (SQL, XSS, JWT)
│   ├── 10-keyboard-optimizer.js   # Keyboard callback_data
│   ├── 11-db-optimizer.js      # Индексы и оптимизация DB
│   ├── 12-session-manager.js   # Управление сессиями (auto-fix)
│   ├── 13-input-validator.js   # Валидация вводов пользователя
│   ├── 14-markdown-safety.js   # Безопасность Markdown
│   ├── 15-error-recovery.js    # Обработка ошибок
│   ├── 16-photo-handler.js     # Работа с фото
│   ├── 17-search-enhancer.js   # Поиск и фильтры
│   ├── 18-response-formatter.js   # Форматирование ответов
│   ├── 19-pagination-checker.js   # Пагинация списков
│   ├── 20-state-machine.js     # Машина состояний бота
│   ├── 21-admin-protection.js  # Защита admin-функций
│   ├── 22-sql-safety.js        # SQL injection check
│   ├── 23-deeplink-handler.js  # Deep links и Mini App
│   ├── 24-performance-tuner.js # Производительность
│   ├── 25-consistency-checker.js  # Согласованность констант
│   ├── orchestrator.js         # Главный мозг — запускает 25 агентов
│   ├── bug-hunter.js           # Охотник за багами в коде
│   └── run-organism.js         # Master runner всего организма
├── tools/
│   └── notify.js       # CLI для Telegram-уведомлений
├── .env.example        # Шаблон переменных окружения
├── docker-compose.yml  # Docker конфигурация
└── package.json
```

---

## Установка и запуск

### 1. Требования

- Node.js 18+
- npm
- PM2 (для продакшна): `npm install -g pm2`

### 2. Клонирование

```bash
git clone <repo_url>
cd nevesty-models
npm install
```

### 3. Настройка .env

Создай файл `.env` (скопируй из `.env.example`):

```env
# Telegram Bot
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN_HERE
ADMIN_TELEGRAM_IDS=YOUR_TELEGRAM_ID_HERE
BOT_USERNAME=YourBotUsername

# Web
PORT=3000
SITE_URL=https://yourdomain.com
JWT_SECRET=change-this-to-random-string

# Admin panel
ADMIN_PASSWORD=admin123
```

**Как получить токен бота:**
1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → введи имя → получи токен
3. Скопируй в `.env`

**Как получить свой Telegram ID:**
1. Открой [@userinfobot](https://t.me/userinfobot)
2. Нажми Start — получишь свой ID

### 4. Запуск для разработки

```bash
node server.js
```

Бот и веб-сервер запустятся вместе.

### 5. Запуск через PM2 (рекомендуется)

```bash
pm2 start server.js --name nevesty-models --restart-delay=3000 --max-restarts=10
pm2 save
pm2 startup  # для автозапуска при перезагрузке
```

### 6. Docker

```bash
docker-compose up -d
```

---

## Функционал Telegram-бота

### Клиентская часть
- **Главное меню**: Каталог, Бронирование, Статус заказа, О нас
- **Каталог**: фильтры по категориям (All / Fashion / Commercial / Events), пагинация, карточки моделей с фото
- **Бронирование**: 4-шаговый мастер (выбор модели → детали события → контактные данные → подтверждение)
- **Статус заказа**: поиск по номеру заказа
- **Telegram Mini App**: открывает сайт внутри Telegram, автозаполняет форму из профиля пользователя

### Административная часть (доступна только adminам)
- Просмотр заказов с фильтрами по статусам
- Изменение статуса заказа (new → in_review → confirmed → in_progress → completed / rejected)
- Управление моделями (вкл/выкл доступность)
- Переписка с клиентом через бота
- Уведомления о новых заказах

### Статусы заказов
| Статус | Описание |
|--------|----------|
| `new` | Новый заказ |
| `in_review` | На рассмотрении |
| `confirmed` | Подтверждён |
| `in_progress` | В процессе |
| `completed` | Завершён |
| `rejected` | Отклонён |

---

## Система ИИ-агентов (Живой Организм)

25 агентов-программистов постоянно анализируют систему. Каждый агент — это орган живого организма.

### Запуск полного цикла проверки

```bash
cd agents
node run-organism.js
```

Это запустит Bug Hunter + Orchestrator (все 25 агентов). Результаты отправятся в Telegram и запишутся в БД.

### Запуск отдельного агента

```bash
node agents/01-ux-architect.js
node agents/09-security-guard.js
# и т.д.
```

### Автоматический запуск каждые 30 минут (PM2)

```bash
pm2 start agents/run-organism.js --name organism --cron "*/30 * * * *" --no-autorestart
pm2 save
```

### Дашборд агентов

Откройте в браузере: `http://localhost:3000/dashboard/`

---

## Admin Panel

- URL: `http://localhost:3000/admin/login.html`
- Логин: `admin`
- Пароль: `admin123` (измени в `.env` → `ADMIN_PASSWORD`)

---

## API Endpoints

| Method | Path | Описание |
|--------|------|----------|
| GET | `/api/models` | Список моделей |
| GET | `/api/models/:id` | Карточка модели |
| POST | `/api/orders` | Создать заказ |
| GET | `/api/orders/:number` | Статус заказа |
| GET | `/api/agent-logs` | Логи агентов (публичный) |
| POST | `/api/auth/login` | Авторизация в админку |
| GET | `/admin/orders` | Список заказов (auth) |
| PATCH | `/admin/orders/:id/status` | Изменить статус (auth) |

---

## База данных (SQLite)

Файл: `data.db` (создаётся автоматически при первом запуске)

### Таблицы

- **models** — модели агентства
- **orders** — заказы на бронирование
- **telegram_sessions** — состояния диалогов в боте
- **agent_logs** — логи работы ИИ-агентов

---

## Telegram Mini App

Сайт открывается внутри Telegram как Mini App. Для полной работы нужен HTTPS.

1. Настройте HTTPS (nginx + certbot или Cloudflare)
2. Укажите в `.env`: `SITE_URL=https://yourdomain.com`
3. В боте появятся кнопки, открывающие сайт внутри Telegram
4. Форма бронирования автоматически заполняется данными из Telegram

---

## Для Lovable / деплоя

Если вы открываете этот проект в Lovable или деплоите на сервер:

1. Установите Node.js 18+
2. Запустите `npm install`
3. Создайте `.env` по шаблону выше
4. Запустите `node server.js`
5. Бот работает в режиме polling — не нужен HTTPS для базовой работы
6. Для Mini App и продакшна — настройте HTTPS и proxy через nginx

### Важно: бот работает на вашем сервере (polling mode)

Telegram-бот использует polling — он сам запрашивает обновления у серверов Telegram. Значит:
- Бот должен работать 24/7 (используйте PM2 или Docker)
- HTTPS не обязателен для бота, но нужен для Mini App
- Бот и веб-сервер запускаются одним процессом (`server.js`)
