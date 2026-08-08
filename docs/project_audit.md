# BASIC.FOOD — Аудит проекту
> Дата: 2026-05-12 | Проведено: Pablo AI Architect

---

## 1. Виявлений стек

### Frontend
| Шар | Технологія | Версія |
|---|---|---|
| Фреймворк | React | 18.3.1 |
| Збірник | Vite + SWC | 5.4.19 |
| Мова | TypeScript | 5.8.3 |
| UI Kit | shadcn/ui + Radix UI | — |
| Стилі | Tailwind CSS | 3.4.17 |
| Стан/запити | TanStack React Query | 5.83.0 |
| Роутинг | React Router DOM | 6.30.1 |
| Форми | React Hook Form + Zod | — |
| i18n | i18next (UA + EN) | 26.0.5 |
| Графіки | Recharts | 2.15.4 |
| Анімації | tailwindcss-animate | — |
| PDF | jsPDF + AutoTable | — |
| Native | Capacitor 8 (Android APK) | — |

### Backend (Supabase)
| Шар | Технологія |
|---|---|
| БД | PostgreSQL 16 (via Supabase) |
| Auth | Supabase Auth (email + Google + Telegram + Apple) |
| Edge Functions | Deno (TypeScript, 199 функцій) |
| Storage | Supabase Storage |
| Realtime | Supabase Realtime (чати клієнтів) |
| Cron | pg_cron (scheduled автоматизації) |
| ORM | Немає — прямий supabase-js client |

### Інтеграції
| Сервіс | Призначення |
|---|---|
| Nova Poshta API | Доставка, трекінг, відділення |
| Monobank | Оплата (QR / webhook) |
| WayForPay | Альтернативна оплата |
| Telegram Bot API | Бот замовлень, підтримки, розсилок |
| Google Analytics 4 | Поведінкова аналітика |
| Meta Pixel | Facebook/Instagram реклама |
| TikTok Pixel | TikTok реклама |
| Together AI | LLM (Llama 3.3 70B) |
| Cohere | LLM (Command R+) |
| Google AI (Gemini) | LLM fallback |
| Groq | LLM швидкий fallback |
| OpenRouter | LLM останній резерв |
| Remotion | Автогенерація відео |
| MARQ | Брендований дизайн |
| Capacitor Push | Push-сповіщення (Android) |
| IndexNow | Швидка SEO-індексація |

### AI/Automation
| Компонент | Деталі |
|---|---|
| ACOS | ~100 Supabase Edge Functions, pg_cron |
| AI Router | Multi-provider (Together→Cohere→Gemini→Groq→OpenRouter) |
| Agent Runs | `agent_runs` таблиця для моніторингу |
| AI Memory | `ai_memory` таблиця (learned patterns) |
| AI Insights | `ai_insights` таблиця (actionable insights) |
| Tribunal | Система AI-рішень з аргументами/вердиктами |
| Neural Network | `agent_neural_pathways` — авто-виявлення послідовностей |
| Autonomy Config | `agent_autonomy_config` — контроль ризику дій |

---

## 2. Структура бази даних (ключові таблиці)

### Бізнес-ядро
```
products            — каталог (SKU, price, stock_quantity, categories, weight)
orders              — замовлення (status, total_kopecks, customer_*, source)
order_items         — позиції замовлень
customers           — CRM (lifecycle_stage, total_spent, tags, telegram_chat_id)
profiles            — Auth профілі (is_wholesale, is_banned)
user_roles          — RBAC (admin / moderator / user)
```

### Маркетинг
```
promo_codes         — промокоди з умовами
promotions          — акції
smart_bundles       — "часто купують разом"
referral_codes      — реферальна програма
broadcasts          — Telegram розсилки
bot_sequences       — Telegram автоматичні послідовності
loyalty_tiers       — програма лояльності
```

### AI/Автоматизація
```
ai_insights         — підказки від ACOS
ai_memory           — засвоєні патерни (confidence score)
ai_actions          — лог виконаних дій
agent_runs          — моніторинг виконання агентів
agent_action_log    — аудит-трейл (before/after state)
agent_autonomy_config — ліміти та дозволи агентів
agent_neural_pathways — виявлені послідовності агентів
tribunal_cases      — AI-суд для спірних рішень
spawned_agents      — субагенти (lifecycle управління)
```

### Аналітика
```
events              — ACOS events (60+ типів)
cac_ltv_snapshots   — щоденні KPI
customer_ltv        — LTV по клієнтах
customer_ltv_scores — ML scores
price_experiments   — A/B тести цін
inventory_forecasts — прогноз запасів
stockout_losses     — втрати від відсутності товару
```

---

## 3. Checkout Flow

```
Catalog → Product Page → Add to Cart (CartContext localStorage)
→ Checkout.tsx → Nova Poshta Picker → Promo Code Apply
→ create_order_with_items() RPC (atomic: order + items + bundle discount + experiment attribution)
→ Payment (Monobank QR / WayForPay / COD)
→ Monobank webhook → payment_status update
→ OrderSuccess.tsx → acos.purchase_completed event
→ Lifecycle triggers (review request, reorder reminder, etc.)
```

**Ключові особливості:**
- Гостевий checkout (без реєстрації, access_token для перегляду)
- Черновик форми в localStorage (24h TTL)
- Bundle discount автоматично в RPC
- Price A/B experiments прив'язані до session_id
- Preferred delivery date picker

---

## 4. Адмін-панель

50+ сторінок, організованих у групи:
- Початок (дашборд, AI-підказки, сповіщення)
- Продажі (замовлення, накладні, акції)
- Каталог і запаси (товари, прогнози, bundle, price lab)
- Клієнти (CRM, когорти, LTV, churn, чат)
- Маркетинг (розсилки, lifecycle, win-back, реферали)
- Контент та SEO
- Telegram Bot
- AI/Агенти (health dashboard, memory console, actions log)
- Фінанси (P&L, FOP книга)
- Система (debug, self-healing, tribunal)

---

## 5. Слабкі місця та технічний борг

### КРИТИЧНІ
| # | Проблема | Вплив |
|---|---|---|
| 1 | **Немає Claude/Anthropic** — AI Router використовує тільки безкоштовні/дешеві моделі | Низька якість стратегічних рішень |
| 2 | **Немає виконавчого AI шару** — агенти операційні, але немає CEO/CMO/CFO рівня | Засновник — єдина стратегічна точка |
| 3 | **Автоматичний Telegram-бот без LLM** — rule-based auto-reply | Незадовільна підтримка клієнтів |
| 4 | **Немає вектор-пам'яті** — `ai_memory` зберігає тільки ключ-значення | Агенти не можуть семантично шукати |
| 5 | **Approval workflow не завершений** — agent_action_log є, але UI для підтвердження відсутній | Ризик авто-дій без контролю |

### СЕРЙОЗНІ
| # | Проблема |
|---|---|
| 6 | Немає єдиного "Health Score" бізнесу — метрики розкидані по 10+ сторінках |
| 7 | pg_cron задачі не мають retry logic — якщо edge function падає, ніхто не знає |
| 8 | AI insights не мають пріоритизації — засновник бачить 50 insights без ранжування |
| 9 | Немає P&L в реальному часі — фінансова сторінка вимагає ручного заповнення |
| 10 | Nova Poshta відмови не аналізуються автоматично (refusal analytics відсутня) |

### ОПТИМІЗАЦІЙНІ
| # | Проблема |
|---|---|
| 11 | Немає UTM attribution всередині Supabase (Meta/Google clicks не зв'язані з замовленнями) |
| 12 | Email-канал повністю відсутній (тільки Telegram) |
| 13 | Реферальна програма не промотується автоматично VIP клієнтам |
| 14 | A/B pricing experiments не мають авто-winner-оголошення |
| 15 | Cart recovery через бота є, але email cart recovery відсутня |

---

## 6. Можливості для інтеграції (Pablo AI)

### Пріоритет 1 — Claude Executive Brain
Додати Claude Opus 4.7 як "мозок" виконавчого рівня:
- Синтез всіх ACOS insights → 1 стратегічне рішення на день
- CEO/CMO/CFO агенти з реальним розумінням бізнесу
- Ранкові брифінги з чітким планом дій

### Пріоритет 2 — Claude Customer Support
Замінити rule-based auto-reply на Claude-powered агента:
- Природна мова, розуміння контексту замовлень
- Ескалація до людини при потребі
- Пам'ять попередніх розмов

### Пріоритет 3 — Approval Dashboard  
UI для підтвердження ризикових AI-дій:
- Нова сторінка `/admin/pablo-ai`
- Approval/reject з поясненням
- Аудит-трейл рішень

### Пріоритет 4 — Nova Poshta Intelligence
Повна аналітика відмов та доставки:
- % відмов по містах, операторах
- Авто-blacklist проблемних адрес
- Прогноз delivery success rate

### Пріоритет 5 — Email Channel
Додати email як канал (поряд з Telegram):
- Transactional emails (order confirmation, tracking)
- Win-back кампанії для клієнтів без Telegram

---

## 7. Архітектура ACOS (що вже є)

```
pg_cron → Edge Function → ai-router (Together/Cohere/Gemini/Groq)
                        → supabase (read/write)
                        → Telegram Bot API
                        → ai_insights (write)
                        → agent_runs (log)
                        → agent_action_log (audit)
```

**Активні cron jobs (приблизно):**
- Щогодини: cart recovery, lifecycle, anomaly detection
- Кожні 6h: inventory forecast, customer segments
- Щодня 06:00 UTC: daily digest, margin analysis
- Щотижня: cohort analysis, LTV update

---

## 8. Продуктовий каталог

**Категорія**: Натуральні повітряно-сушені ласощі з яловичини
**Продукти** (з assets): легені, серце, вим'я, нирки, аорта, стравохід, трахея, рубець, печінка, мікс
**Одиниці**: 100г, 200г, 500г пачки
**Ціни**: зберігаються в копійках (UAH × 100)
**Категорії товарів**: теги, B2C + B2B wholesale (окремі ціни)

---

## 9. Висновок

BASIC.FOOD — один з найбільш технічно розвинених Ukrainian DTC e-commerce проектів. Система ACOS вже виконує 100+ автоматизацій, але бракує:

1. **Стратегічного AI шару** (Claude Opus 4.7 як CEO brain)
2. **Якісної підтримки клієнтів через LLM**
3. **Зрозумілого approval workflow для ризикових дій**
4. **Єдиного "Health Score" дашборду**

Pablo AI додає саме ці шари поверх існуючої ACOS інфраструктури.
