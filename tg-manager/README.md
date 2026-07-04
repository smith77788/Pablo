# Infragram — Telegram Management Platform

Telegram-native infrastructure and mass-action operating system. Maximum Telegram capabilities. Minimum manual work.

## Core Features

### Mass Operations
- Bulk join/leave/invite/DM
- Mass publish with templates and scheduling
- Bulk channel editing (username, about, photo)
- Strike engine (12-vector attack: join → history scan → repost → report → block)

### Account Management
- QR login, phone login, session import (StringSession/Pyrogram/tdata)
- Proxy binding, tags, pools, roles
- Health score, activity history, warmup system
- Auto-rotation, ecosystem and regional assignment

### Ecosystem Management
- Multi-geo support with regional clusters
- Topology map and asset grouping
- Global Presence Factory (template → geo preset → username engine → distribution)

### AI Assistant
- Natural language → Telegram action
- OpenRouter backend with rate limiting
- Semantic memory and intent planner

### Spintax Randomizer (`/spin`)
- Turns a plain script into 5 maximally-randomized spintax templates via the
  already-connected LLM providers (OpenRouter/Groq/Gemini/Ollama)
- Synonym groups `{вариант1|вариант2}`, nesting, matching capitalization,
  no more than two unspun words in a row
- Every template is validated by the vendored engine
  (`services/spintax_engine/`); ready templates expand into random variants
- `🔁 Ещё генерация` re-rolls the stored templates instantly, without another
  LLM call
- Uses strong models by default (DeepSeek V3 / Llama-3.3-70B on OpenRouter) to
  avoid junk from tiny models; override with `SPIN_MODELS` (comma-separated
  OpenRouter models)

### Billing & Subscriptions
- Crypto payments (TON, TRON)
- Plan management with usage-based pricing
- Approval workflows and RBAC

---

## Advanced Engine Features

### Adaptive Pacing (`services/flood_engine.py`)
Dynamic delay adjustment based on Telegram's response patterns. The engine tracks FloodWait events and automatically increases inter-action delays when restrictions are detected, then gradually reduces them during stable operation. Per-action baselines (join: 55s, invite: 90s, strike: 240s) ensure conservative pacing for high-risk operations.

### Circuit Breaker (`services/flood_engine.py`)
Automatic operation pausing when consecutive failures exceed threshold. Prevents cascade failures and account bans by halting operations and requiring manual review or cooldown period before resumption.

### Smart Retry (`services/op_worker.py`)
Intelligent retry logic handling Telegram-specific errors:
- **FloodWait**: waits exact duration specified by Telegram
- **PeerFlood**: exponential backoff with per-account tracking
- **CHANNEL_PRIVATE**: removes channel from queue, logs for review
- Preserves operation state across retries with automatic recovery

### Human-like Behavior (`services/session_simulator.py`)
Behavioral timing simulation to avoid bot detection:
- Beta-distributed delays between actions (skewed toward realistic values)
- Typing simulation (~50-100ms per character, capped at 8s)
- Micro-jitter between rapid actions
- Bulk item pausing with batch-boundary breaks
- Chaos factor for timing variation

### CSV/JSON Export
Data export functionality for:
- Audience/member lists
- Operation reports and analytics
- Account health metrics
- Ecosystem statistics

### Push Notifications (`services/deploy_notifier.py`)
Automatic admin notifications for:
- New deployments with changelog
- Operation completion with summary
- Critical errors and health alerts
- Payment confirmations

### Proxy Intelligence (`services/proxy_selector.py`)
Unified proxy management and scoring:
- Quality scoring from infra_memory
- IP diversity validation (prevents datacenter bans)
- Datacenter IP range detection (Railway, AWS, GCP, Azure, etc.)
- Per-proxy performance tracking

### Account Rotation (`services/account_manager.py`, `services/resource_selector.py`)
Intelligent account selection for operations:
- Trust score-based rotation
- Cooldown management between operations
- Pool-based distribution
- Health-aware scheduling

### Ecosystem Analysis (`services/ecosystem_brain.py`)
Central intelligence for ecosystem health:
- Health Score (account, proxy, operation reliability)
- Pressure Score (load, density, congestion)
- Risk Assessment (operational, infrastructure, account, proxy, recovery)
- Drift Detection (deviation from templates)

### Usage-based Pricing (`services/account_budget.py`)
Daily action budgets per account:
- Conservative defaults (50 actions/day)
- Per-action-type tracking
- Budget exhaustion detection
- Configurable via platform settings

### Rate Limiting (`services/api_rate_limiter.py`)
Per-user request throttling:
- Configurable window and max requests
- Sliding window counter
- Automatic cleanup of expired entries

### In-Memory Caching (`services/cache.py`)
TTL-based caching layer:
- Configurable TTL (default 5 minutes)
- LRU-style eviction at capacity
- Hit/miss statistics
- Decorator support for function caching

---

## Architecture

```
tg-manager/
├── main.py                 # Bot entry point
├── config.py               # Environment configuration
├── database/               # PostgreSQL schema and queries
├── bot/
│   ├── handlers/           # Telegram command/callback handlers
│   ├── middlewares/        # Request processing middleware
│   └── utils/              # Helper functions
└── services/               # Business logic engines
    ├── flood_engine.py     # Adaptive pacing, circuit breaker
    ├── op_worker.py        # Operation execution engine
    ├── session_simulator.py # Human-like behavior
    ├── proxy_selector.py   # Proxy intelligence
    ├── ecosystem_brain.py  # Ecosystem analysis
    ├── account_manager.py  # Account management
    ├── cache.py            # In-memory caching
    ├── api_rate_limiter.py # Rate limiting
    └── ...
```

## Configuration

Environment variables (see `config.py`):

| Variable | Description | Required |
|----------|-------------|----------|
| `MANAGER_BOT_TOKEN` | Telegram bot token | Yes |
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `ADMIN_IDS` | Comma-separated admin Telegram IDs | Yes |
| `TG_API_ID` | Telegram API ID (for Telethon) | Yes |
| `TG_API_HASH` | Telegram API hash | Yes |
| `TG_PROXY` | SOCKS5 proxy for Telethon | No |
| `CF_RELAY_URL` | Cloudflare Worker WebSocket relay | No |
| `TON_WALLET` | TON wallet for payments | No |
| `TRON_WALLET` | TRON wallet for USDT payments | No |
| `OPENROUTER_API_KEY` | OpenRouter API key for AI | No |
| `OPENROUTER_MODEL` | AI model (default: claude-sonnet-4-6) | No |

## Deployment

Infragram runs on Railway with PostgreSQL. Auto-deploys from `claude/telegram-bot-services-xfAh6` branch.

```bash
# Local development
cp .env.example .env  # Fill in credentials
pip install -e .
python -m tg-manager.main
```

## Tech Stack

- Python 3.12
- aiogram 3.x (Telegram Bot API)
- Telethon (Telegram Client API)
- asyncpg (PostgreSQL)
- aiohttp (HTTP client)
- OpenRouter (AI backend)
