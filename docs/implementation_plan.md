# Pablo AI — План впровадження
> Покроковий план для BASIC.FOOD

---

## Кроки для запуску (в порядку виконання)

### Крок 1 — Налаштування змінних оточення (15 хв)

1. Отримати `ANTHROPIC_API_KEY` на console.anthropic.com
2. У Supabase Dashboard → Settings → Edge Functions → Secrets, додати:
   ```
   ANTHROPIC_API_KEY = sk-ant-...
   ```
3. Переконатися що вже є (мають бути):
   - `TELEGRAM_API_KEY` 
   - `SUPABASE_SERVICE_ROLE_KEY` (автоматично в edge functions)

---

### Крок 2 — Міграція БД (5 хв)

1. Відкрити Supabase Dashboard → SQL Editor
2. Виконати `supabase/migrations/001_pablo_ai_tables.sql`
3. Перевірити що таблиці створились:
   ```sql
   SELECT table_name FROM information_schema.tables 
   WHERE table_name LIKE 'pablo_%';
   ```

---

### Крок 3 — Deploy Edge Functions (10 хв)

Якщо використовуєш Supabase CLI:
```bash
supabase functions deploy pablo-executive-brain
supabase functions deploy pablo-morning-brief
supabase functions deploy pablo-support-agent
```

Або через Lovable Cloud якщо не маєш CLI — скопіюй код функцій в Supabase Dashboard → Edge Functions.

---

### Крок 4 — Налаштування pg_cron (5 хв)

1. Відкрити Supabase Dashboard → SQL Editor
2. Виконати `supabase/migrations/002_pablo_cron_jobs.sql`
3. Перевірити задачі:
   ```sql
   SELECT jobname, schedule, active FROM cron.job WHERE jobname LIKE 'pablo%';
   ```

---

### Крок 5 — Інтеграція в React Admin Panel (30 хв)

1. Скопіювати `src/pages/admin/AdminPabloAI.tsx` в твій проект
2. Додати залежності якщо немає:
   ```bash
   bun add react-markdown remark-gfm
   ```
3. У `src/App.tsx` додати:
   ```tsx
   const AdminPabloAI = lazy(() => import("./pages/admin/AdminPabloAI"));
   // В /admin routes:
   <Route path="pablo-ai" element={<ErrorBoundary label="Pablo AI"><AdminPabloAI /></ErrorBoundary>} />
   ```
4. У `src/components/AdminLayout.tsx` додати в навігацію (див. `src/patches/AdminLayout.patch.md`)

---

### Крок 6 — Тестування (15 хв)

1. Відкрити `/admin/pablo-ai`
2. Вибрати агента CEO
3. Ввести: "Проаналізуй стан бізнесу за останній тиждень"
4. Отримати відповідь від Claude
5. Натиснути "Ранковий брифінг" → перевірити в Telegram

---

### Крок 7 — Інтеграція підтримки (для Telegram бота)

У вашому існуючому bot webhook (edge function яка обробляє Telegram updates):
```typescript
// Замість rule-based auto-reply:
const { data } = await supabase.functions.invoke("pablo-support-agent", {
  body: { 
    chat_id: message.chat.id, 
    user_message: message.text,
    user_name: message.from?.first_name 
  }
});
// data.reply вже відправлено в Telegram
```

---

## Архітектура файлів у проекті basic-food.shop

```
src/
  pages/admin/
    AdminPabloAI.tsx        ← нова сторінка (скопіювати)
  
  components/AdminLayout.tsx ← додати Pablo AI в навігацію
  App.tsx                    ← додати route pablo-ai

supabase/
  migrations/
    001_pablo_ai_tables.sql  ← виконати в SQL Editor
    002_pablo_cron_jobs.sql  ← виконати в SQL Editor
  
  functions/
    pablo-executive-brain/
      index.ts               ← deploy до Supabase
    pablo-morning-brief/
      index.ts               ← deploy до Supabase
    pablo-support-agent/
      index.ts               ← deploy до Supabase
```

---

## Результат після впровадження

| Функція | Де видно |
|---|---|
| Розмова з CEO/CMO/CFO/COO | `/admin/pablo-ai` → вкладка Агенти |
| Підтвердження ризикових рішень | `/admin/pablo-ai` → вкладка Підтвердження |
| Ранковий брифінг | Telegram о 09:00 + `/admin/pablo-ai` → Брифінг |
| Журнал рішень | `/admin/pablo-ai` → Журнал |
| Claude підтримка клієнтів | Автоматично в Telegram боті |
| Тижневий звіт | Telegram щопонеділка + ai_insights |

---

## Вартість (Anthropic API)

| Операція | Токени | Вартість (USD) |
|---|---|---|
| Ранковий брифінг (1/день) | ~3000 | ~$0.075 |
| Агент-запит CEO (за запит) | ~2000 | ~$0.05 |
| Підтримка клієнта (за повідомлення) | ~800 | ~$0.02 |
| **Місяць (актив. використання)** | **~200K** | **~$4-10** |

> Anthropic Claude Opus 4.7: $5/1M input tokens, $25/1M output tokens

---

## Що ще можна додати (Phase 2)

1. **Nova Poshta Agent** — автоматичний трекінг відправлень через NP API
2. **Vector Memory** — pgvector для семантичного пошуку по клієнтах і розмовах
3. **Email Channel** — Claude агент для email підтримки (додати до pablo-support-agent)
4. **CFO Alert** — авто-сповіщення при падінні маржі нижче порогу
5. **CMO Campaign Generator** — Claude генерує тексти для Telegram broadcasts
