# Pablo AI — Архітектура виконавчої AI системи
> Версія 1.0 | Інтеграція в BASIC.FOOD

---

## Концепція

Pablo — **виконавчий AI шар** поверх існуючого ACOS. Якщо ACOS — це нервова система (реагує на сигнали), то Pablo — це мозок (думає стратегічно).

```
┌─────────────────────────────────────────────────┐
│              PABLO EXECUTIVE BRAIN               │
│  (Claude Opus 4.7 + adaptive thinking)          │
│                                                  │
│  CEO · CMO · CFO · COO · Analyst · CoS          │
└──────────────────┬──────────────────────────────┘
                   │ reads + acts
┌──────────────────▼──────────────────────────────┐
│              EXISTING ACOS SYSTEM                │
│  (100+ Edge Functions, pg_cron, ai_insights)    │
│  ai_insights · agent_runs · events · orders     │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│              SUPABASE DATABASE                   │
│  PostgreSQL · Auth · Storage · Realtime         │
└─────────────────────────────────────────────────┘
```

---

## Рівні рішень

### Рівень 1 — Автоматичне виконання (LOW RISK)
Виконується без підтвердження:
- Генерація звітів та дашбордів
- Аналітика та рекомендації (запис в `ai_insights`)
- Оновлення інформаційних полів (`notes`, `tags`)
- Відправка алертів (Telegram до засновника)
- Відповіді на стандартні запити підтримки

### Рівень 2 — Підтвердження засновника (MEDIUM RISK)
Чекає схвалення в `/admin/pablo-ai`:
- Запуск маркетингових кампаній
- Знижки та промокоди
- Масові розсилки (Telegram broadcast)
- Зміна lifecycle stage клієнта
- Додавання до blacklist

### Рівень 3 — Суворе підтвердження (HIGH RISK)
Потребує двоетапного підтвердження + обґрунтування:
- Зміна цін на продукти
- Деактивація продуктів
- Зміна бюджетів
- Зміна умов доставки
- Видалення даних

---

## Виконавчі агенти

### CEO Agent
**Ціль**: Стратегічне планування, пріоритизація, рішення
**Тригери**: Ранковий брифінг, тижневий огляд, аномалії
**Інструменти**: Всі read-only + approve/reject decisions
**Prompt архетип**: Досвідчений CEO DTC бренду, думає про unit economics, growth levers, positioning

### CMO Agent  
**Ціль**: Маркетингова стратегія, кампанії, контент
**Тригери**: Падіння ROAS, зниження трафіку, нові продукти
**Інструменти**: broadcast_draft, content_proposals, promo_codes (draft)
**Спеціалізація**: Знає Ukraine ринок, Telegram-first, Nova Poshta dynamics

### CFO Agent
**Ціль**: Фінанси, unit economics, прибутковість
**Тригери**: Маржа < порогу, аномальні витрати, кінець місяця
**Інструменти**: finance_transactions read, margin analysis, budget alerts
**Метрики**: CAC, LTV, ROAS, Gross Margin, Contribution Margin

### COO Agent
**Ціль**: Операції, логістика, виконання замовлень
**Тригери**: Затримки доставки, % відмов Nova Poshta, низький запас
**Інструменти**: orders read/update, inventory, Nova Poshta API
**Спеціалізація**: Ukrainian logistics, Nova Poshta відмови, fulfillment optimization

### Chief of Staff Agent
**Ціль**: Координація агентів, роутинг завдань, звітність
**Тригери**: Кожен запит до Pablo
**Інструменти**: Виклик інших агентів, `pablo_tasks` write, нотифікації
**Роль**: Gateway між засновником та спеціалізованими агентами

### Analyst Agent
**Ціль**: Аналіз KPI, когорти, прогнози
**Тригери**: Запит на аналіз, тижневий звіт
**Інструменти**: events, orders, customers, cohorts, LTV (read)
**Вихід**: Структуровані Markdown-звіти з висновками та рекомендаціями

---

## Операційні агенти

### Підтримка клієнтів (Support Agent)
- Claude Opus 4.7 замість rule-based auto-reply
- Читає `customers`, `orders`, `reviews`, `pet_profiles`
- Відповідає в Telegram (існуючий бот)
- Пам'ять розмови через `chat_sessions`
- Ескалація → нотифікація засновника

### Nova Poshta Agent
- Відстеження відправлень
- Виявлення відмов та невдалих доставок
- Аналіз по містах, відділеннях
- Авто-зв'язок з клієнтом при відмові

### Inventory Agent
- Моніторинг stock_quantity
- Прогноз stockout на основі velocity
- Авто-alert при критичному рівні
- Рекомендації до закупівлі

### Marketing Automation Agent
- Запуск win-back кампаній
- Cart recovery Telegram messages
- Reorder reminders для subscription-type клієнтів
- A/B тест повідомлень

---

## Memory System

### Short-term Memory (Redis-like)
- Контекст поточної розмови (chat session)
- Стан поточного завдання агента
- Реалізація: `pablo_session_context` (Supabase таблиця, TTL 24h)

### Long-term Memory (Structured)
- Існуюча таблиця `ai_memory` (patterns, confidence)
- `pablo_executive_memory` — стратегічні рішення CEO/CMO/CFO
- `pablo_customer_profiles` — AI-generated customer summaries

### Semantic Memory (Vector)
- `pablo_embeddings` — векторні ембедінги клієнтів, продуктів, розмов
- Використовується для: схожі запити підтримки, релевантні продукти, схожі клієнти
- Реалізація: pgvector (Supabase вже підтримує)

---

## Технічна реалізація

### Нові Supabase Edge Functions
```
pablo-executive-brain    — головний Claude агент, routing
pablo-support-agent      — Claude-powered підтримка
pablo-morning-brief      — щоденний брифінг (+ Claude synthesis)
pablo-nova-poshta        — відстеження та аналіз відправлень
pablo-approval-handler   — обробка approve/reject від засновника
```

### Нові React Admin Pages
```
/admin/pablo-ai          — головна сторінка Pablo AI
/admin/pablo-approvals   — approval queue
/admin/pablo-memory      — executive memory viewer
/admin/pablo-agents      — статус агентів Pablo
```

### Нові Supabase таблиці
```
pablo_executive_decisions  — рішення виконавчих агентів
pablo_approval_queue       — черга підтвердження (засновник)
pablo_executive_memory     — довгострокова пам'ять агентів
pablo_session_context      — контекст розмов підтримки
pablo_embeddings           — векторні ембедінги (pgvector)
```

---

## Інтеграція з існуючим ACOS

Pablo **не замінює** ACOS — він **читає його вихід** і приймає вищорівневі рішення:

```
ACOS generates → ai_insights (50+ daily insights)
                            ↓
Pablo CEO Agent reads → synthesizes → "Top 3 actions today"
                                    → writes pablo_executive_decisions
                                    → sends to /admin/pablo-ai
                                    → Telegram to founder
```

Аналогічно:
- ACOS виявляє аномалію → Pablo CFO аналізує → рекомендація + alert
- ACOS запускає cart recovery → Pablo CMO оцінює → approve/adjust messaging
- ACOS inventory alert → Pablo COO приймає рішення → procurement action

---

## Модель безпеки

- Pablo використовує `SUPABASE_SERVICE_ROLE_KEY` (є в edge functions env)
- Всі HIGH RISK дії логуються в `agent_action_log` (існує)
- `agent_autonomy_config` контролює ліміти (exists)
- Approval queue в `pablo_approval_queue` видно засновнику
- Повний audit trail через `pablo_executive_decisions`

---

## Deployment

Pablo запускається як:
1. **Supabase Edge Functions** (для real-time тригерів і UI-викликів)
2. **Python workers** (для складних багатокрокових завдань через pablo/)
3. **pg_cron** (для scheduled завдань — ранковий брифінг тощо)
