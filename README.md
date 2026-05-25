# Pablo AI — Autonomous Executive Operating System

**Pablo** is an AI-powered executive layer for [BASIC.FOOD](https://basic-food.shop) — a Ukrainian pet treats e-commerce platform. Built on Claude Opus 4.7, Pablo handles strategic decision-making, customer support, and business intelligence.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│           PABLO EXECUTIVE BRAIN                      │
│   CEO · CMO · CFO · COO · CoS · Analyst            │
│              Claude Opus 4.7                        │
└──────────────────┬──────────────────────────────────┘
                   │ reads + acts
┌──────────────────▼──────────────────────────────────┐
│           EXISTING ACOS SYSTEM                       │
│   ai_insights · agent_runs · events · orders        │
│           100+ Supabase Edge Functions              │
└──────────────────┬──────────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│           SUPABASE DATABASE                          │
│        basic-food.shop production                   │
└─────────────────────────────────────────────────────┘
```

### Risk-Based Approval

| Risk Level | Action |
|---|---|
| `LOW` | Auto-execute immediately |
| `MEDIUM` | → `pablo_approval_queue` (founder review) |
| `HIGH` | → `pablo_approval_queue` + Telegram alert |

---

## Components

### Python Backend (`/`)

Standalone agent runtime for local execution and cron jobs.

| File | Purpose |
|---|---|
| `main.py` | CLI entry point |
| `orchestrator.py` | Pablo coordinator (Telegram loop, briefings, orders) |
| `agents/base.py` | BaseAgent with Claude tool loop + streaming |
| `agents/customer_support.py` | Ukrainian-language support agent |
| `agents/order_manager.py` | Nova Poshta tracking, order updates |
| `agents/analytics.py` | Daily/weekly revenue reports |
| `agents/inventory.py` | Stock monitoring and alerts |
| `database/models.py` | Supabase client singleton |
| `tools/database_tools.py` | CRM/order/product Supabase tools |
| `tools/analytics_tools.py` | Revenue analytics (UAH, kopecks→UAH) |
| `tools/telegram_tools.py` | Bot polling and message dispatch |
| `tools/email_tools.py` | IMAP/SMTP email handling |

### Supabase Edge Functions (`/supabase/functions/`)

Deno TypeScript functions deployed to Supabase.

| Function | Purpose |
|---|---|
| `pablo-executive-brain` | Main agent router — CEO/CMO/CFO/COO/CoS/Analyst |
| `pablo-morning-brief` | Daily 09:00 Kyiv briefing → Telegram |
| `pablo-support-agent` | Claude-powered Telegram customer support |

### React Admin Page (`/src/pages/admin/AdminPabloAI.tsx`)

New admin panel at `/admin/pablo-ai` with 4 tabs:

- **Агенти** — Chat with any executive agent
- **Підтвердження** — Approve/reject pending decisions
- **Брифінг** — Latest morning brief
- **Журнал** — Decision history log

### Database Migrations (`/supabase/migrations/`)

| File | Contents |
|---|---|
| `001_pablo_ai_tables.sql` | 5 new tables + RLS + helper functions |
| `002_pablo_cron_jobs.sql` | pg_cron: morning brief, expire approvals, weekly report |

---

## Quick Start

### 1. Environment Variables

```bash
cp .env.example .env
# Fill in:
# ANTHROPIC_API_KEY=sk-ant-...
# SUPABASE_URL=https://xxx.supabase.co
# SUPABASE_SERVICE_KEY=eyJ...
# TELEGRAM_BOT_TOKEN=...
```

In Supabase Dashboard → Settings → Edge Functions → Secrets, add `ANTHROPIC_API_KEY`.

### 2. Database Migration

```sql
-- Run in Supabase SQL Editor:
-- 1. supabase/migrations/001_pablo_ai_tables.sql
-- 2. supabase/migrations/002_pablo_cron_jobs.sql
```

### 3. Deploy Edge Functions

```bash
supabase functions deploy pablo-executive-brain
supabase functions deploy pablo-morning-brief
supabase functions deploy pablo-support-agent
```

### 4. Python Backend

```bash
pip install -r requirements.txt

# Run Telegram bot loop
python main.py telegram

# Send morning briefing
python main.py briefing

# Process new orders
python main.py orders

# Check stock levels
python main.py stock

# Ask any question
python main.py ask "Який стан запасів яловичої легені?"
```

### 5. React Integration

Add to `src/App.tsx`:
```tsx
const AdminPabloAI = lazy(() => import("./pages/admin/AdminPabloAI"));
// In /admin routes:
<Route path="pablo-ai" element={<ErrorBoundary label="Pablo AI"><AdminPabloAI /></ErrorBoundary>} />
```

See `src/patches/AdminLayout.patch.md` for navigation integration.

---

## New Database Tables

```sql
pablo_executive_decisions  -- Agent decisions with risk_level, reasoning
pablo_approval_queue       -- Founder approval workflow
pablo_executive_memory     -- Strategic long-term memory (importance 1-10)
pablo_support_sessions     -- Claude conversation context per chat_id
pablo_briefings            -- Morning/weekly briefings archive
```

---

## Agents

### Executive Layer (Claude Opus 4.7)

| Agent | Role |
|---|---|
| **CEO** | Strategy, priorities, weekly synthesis |
| **CMO** | Marketing, campaigns, customer acquisition |
| **CFO** | Revenue, margins, financial health |
| **COO** | Operations, fulfillment, Nova Poshta |
| **CoS** | Cross-functional coordination |
| **Analyst** | Data analysis, KPI deep-dives |

### Automated Workflows

| Schedule | Job |
|---|---|
| Daily 09:00 Kyiv | Morning briefing → Telegram |
| Every 6 hours | Expire stale approvals |
| Monday 09:00 Kyiv | CEO weekly strategic synthesis |

---

## Estimated Cost (Anthropic API)

| Operation | Tokens | Cost |
|---|---|---|
| Morning briefing (1/day) | ~3,000 | ~$0.075 |
| Executive query | ~2,000 | ~$0.05 |
| Customer support message | ~800 | ~$0.02 |
| **Month (active use)** | **~200K** | **~$4–10** |

Model: Claude Opus 4.7 — $5/1M input, $25/1M output tokens.

---

## Phase 2 Roadmap

1. **Nova Poshta Agent** — automatic shipment tracking via NP API
2. **Vector Memory** — pgvector for semantic search across customers and conversations
3. **Email Channel** — Claude agent for email support + cart recovery
4. **CFO Alerts** — auto-notification when margin drops below threshold
5. **CMO Campaign Generator** — Claude generates Telegram broadcast copy
6. **Full 41-Agent System** — meta_ads, google_ads, seo, content, pricing, bundle, cro, retention, review, refund_prevention, segmentation, procurement, logistics, product_research, forecasting, cohort, ltv_cac agents
