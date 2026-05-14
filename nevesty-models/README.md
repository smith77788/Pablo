# Nevesty Models — Telegram Bot + Modeling Agency Website

> Full-featured modeling agency website + Telegram bot + a system of 16 AI agents for continuous quality improvement.

---

## Project Overview

Nevesty Models is a production-ready platform for a modeling agency. It combines:

- **Website** — public catalog, multi-step booking form, order status lookup
- **Admin panel** — order management, model management, client messaging
- **Telegram bot** — client catalog, 4-step booking flow, admin commands, notifications
- **Telegram Mini App** — the website opens inside Telegram with auto-filled user data
- **Agent system** — 16 specialised AI agents (Security, Reliability, QA, Ops) run automatically on every code change and report findings via Telegram

---

## Requirements

| Requirement | Version |
|-------------|---------|
| Node.js     | 18+     |
| npm         | 8+      |
| PM2         | any (installed by `deploy.sh`) |
| SQLite      | bundled via `sqlite3` npm package |
| OS          | Linux / macOS (Windows via WSL) |

---

## Quick Start

### 1. Clone the repository

```bash
git clone <repo_url>
cd nevesty-models
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env   # fill in your values — see .env Variables below
```

### 3. Run deploy script (recommended)

```bash
./deploy.sh
```

This single command handles all setup steps: dependencies, database init, model seeding, PM2 install/start, and process persistence. See [What deploy.sh does](#what-deploysh-does) for details.

### 4. Manual setup (alternative)

```bash
npm install --production
mkdir -p logs
node database.js          # initialise SQLite schema
node tools/seed-models.js # populate sample models
pm2 start ecosystem.config.js
pm2 save
pm2 startup               # copy-paste the printed sudo command
```

### 5. Development mode

```bash
npm install
node server.js            # or: npx nodemon server.js
```

Open: `http://localhost:3000`

---

## What deploy.sh Does

`deploy.sh` is a fully automated production setup script with bash error handling (`set -e` + `trap`):

| Step | Action |
|------|--------|
| 1 | Warns if `.env` is missing (does not abort) |
| 2 | Runs `npm install --production` |
| 3 | Creates `logs/` directory if absent |
| 4 | Initialises the SQLite database via `node database.js` |
| 5 | Seeds sample models via `node tools/seed-models.js` |
| 6 | Installs PM2 globally if not present |
| 7 | Reloads with `pm2 reload` if already running, else `pm2 start` |
| 8 | Runs `pm2 save` to persist process list across reboots |
| 9 | Prints `pm2 startup` hint for systemd integration |

---

## .env Variables

Copy from `.env.example` and fill in your values:

```env
# ── Server ────────────────────────────────────────────────────────────────
PORT=3000
NODE_ENV=production

# ── Telegram Bot ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=your_bot_token_here          # from @BotFather
BOT_USERNAME=your_bot_username_without_at       # e.g. NvestyModelsBot

# ── Admins ────────────────────────────────────────────────────────────────
# Comma-separated Telegram user IDs (get yours from @userinfobot)
ADMIN_TELEGRAM_IDS=123456789,987654321

# ── Security ──────────────────────────────────────────────────────────────
JWT_SECRET=change-this-to-a-random-64-char-string
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin123                         # change before going live!

# ── Site ──────────────────────────────────────────────────────────────────
SITE_URL=https://yourdomain.com                 # used in bot deep-links & Mini App

# ── Webhook (optional) ────────────────────────────────────────────────────
# If set, the bot switches from polling to webhook mode (requires HTTPS)
WEBHOOK_URL=

# ── Agency contact ────────────────────────────────────────────────────────
AGENCY_PHONE=+7 (800) 555-00-00
AGENCY_EMAIL=info@nevesty-models.ru
```

**How to get your bot token:** open [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

**How to get your Telegram ID:** open [@userinfobot](https://t.me/userinfobot) → Start.

---

## PM2 Commands Reference

```bash
# Status
pm2 status                          # list all processes
pm2 show nevesty-models             # detailed info for one process

# Logs
pm2 logs                            # all processes, live
pm2 logs nevesty-models             # app logs only
pm2 logs nevesty-scheduler          # scheduler logs only
pm2 logs --lines 200                # last 200 lines

# Lifecycle
pm2 start ecosystem.config.js       # start all apps in config
pm2 reload ecosystem.config.js      # zero-downtime reload (recommended)
pm2 restart ecosystem.config.js     # hard restart
pm2 stop ecosystem.config.js        # stop all
pm2 delete ecosystem.config.js      # remove from PM2 list

# Persistence
pm2 save                            # save current process list
pm2 startup                         # print systemd setup command
pm2 unstartup                       # remove systemd integration

# Monitoring
pm2 monit                           # real-time CPU/memory dashboard

# One-off agent run
pm2 start agents/run-organism.js --name organism --no-autorestart
```

---

## Agent System Overview

16 specialised AI agents continuously monitor and improve the codebase. They are organised into four squads that Claude Code launches automatically after every significant change.

### Squads

| Squad | When triggered | Agents |
|-------|---------------|--------|
| **Reliability Squad** | After every code change | Security Auditor, Backend Reliability, Bot Integration, Frontend QA |
| **Fix Squad** | When Reliability Squad finds issues | Fix-Backend, Fix-Frontend, Fix-Bot, Fix-Infra |
| **Quality Squad** | Once per session | Code Reviewer, Accessibility Auditor, SEO Specialist, Performance Engineer |
| **Ops Squad** | Before deploy | DevOps Engineer, Monitoring Engineer, DB Architect, Test Engineer |

### Built-in agents (in `agents/`)

25 always-running bot/system agents check specific subsystems:

```
01-ux-architect       06-admin-experience   11-db-optimizer      16-photo-handler      21-admin-protection
02-booking-complete   07-message-threading  12-session-manager   17-search-enhancer    22-sql-safety
03-model-showcase     08-notification-engine 13-input-validator  18-response-formatter 23-deeplink-handler
04-order-lifecycle    09-security-guard     14-markdown-safety   19-pagination-checker 24-performance-tuner
05-client-experience  10-keyboard-optimizer 15-error-recovery    20-state-machine      25-consistency-checker
```

### Run the full organism

```bash
node agents/run-organism.js
```

Launches Bug Hunter + Orchestrator (all 25 agents). Results go to Telegram and are stored in the database.

### Automatic schedule (PM2 cron)

```bash
pm2 start agents/run-organism.js --name organism --cron "*/30 * * * *" --no-autorestart
pm2 save
```

### Telegram notifications

Agents send real-time updates via the built-in CLI tool:

```bash
node tools/notify.js --from "Agent: DevOps" "✅ Deploy complete"
```

---

## Project Structure

```
nevesty-models/
├── bot.js                      # Telegram bot (booking flow, catalog, admin)
├── server.js                   # Express HTTP server (entry point)
├── database.js                 # SQLite initialisation and helpers
├── ecosystem.config.js         # PM2 process configuration
├── deploy.sh                   # Production setup & deploy script
├── routes/
│   └── api.js                  # REST API
├── middleware/
│   └── auth.js                 # JWT middleware
├── public/
│   ├── index.html              # Homepage
│   ├── catalog.html            # Model catalog
│   ├── booking.html            # 4-step booking form
│   ├── admin/                  # Admin panel
│   ├── dashboard/              # Agent dashboard (React + React Flow)
│   └── js/
│       ├── booking.js          # Booking form logic
│       └── telegram-webapp.js  # Telegram Mini App integration
├── agents/                     # 25 AI agent modules + orchestrator
├── tools/
│   ├── notify.js               # CLI Telegram notifier
│   └── seed-models.js          # Sample model data seeder
├── logs/                       # PM2 logs (created by deploy.sh)
├── uploads/                    # Model photos (served statically)
├── data.db                     # SQLite database (auto-created)
├── .env.example                # Environment variable template
├── docker-compose.yml          # Docker alternative (optional)
└── package.json
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/models` | List all active models |
| GET | `/api/models/:id` | Single model card |
| POST | `/api/orders` | Create a booking order |
| GET | `/api/orders/:number` | Order status lookup |
| GET | `/api/agent-logs` | Agent run logs (public) |
| POST | `/api/auth/login` | Admin login (returns JWT) |
| GET | `/admin/orders` | Order list (JWT required) |
| PATCH | `/admin/orders/:id/status` | Update order status (JWT required) |

---

## Database (SQLite)

File: `data.db` (auto-created on first run via `node database.js`)

| Table | Description |
|-------|-------------|
| `models` | Agency models |
| `orders` | Booking orders |
| `telegram_sessions` | Bot conversation state |
| `agent_logs` | AI agent run history |

---

## Order Statuses

| Status | Description |
|--------|-------------|
| `new` | Just submitted |
| `in_review` | Under review |
| `confirmed` | Confirmed |
| `in_progress` | Service in progress |
| `completed` | Completed |
| `rejected` | Rejected |

---

## Admin Panel

- URL: `http://localhost:3000/admin/login.html`
- Default login: `admin` / `admin123`
- Change credentials in `.env` before going to production

---

## Agent Dashboard

- URL: `http://localhost:3000/dashboard/`
- Shows live agent run results in a React Flow graph

---

## Docker (Alternative)

```bash
docker-compose up -d
docker-compose logs -f app
```

---

## Telegram Mini App

The website opens inside Telegram as a Mini App. Full functionality requires HTTPS.

1. Set up HTTPS (nginx + certbot or Cloudflare Tunnel)
2. Set `SITE_URL=https://yourdomain.com` in `.env`
3. Bot buttons will open the site inside Telegram with auto-filled user data
