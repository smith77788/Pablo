# Architecture Documentation

## System Overview

Infragram is a Telegram-native infrastructure and mass-action operating system. The architecture follows a layered design with clear separation of concerns.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Telegram Bot API                        │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                     aiogram Dispatcher                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  Middlewares  │  │   Handlers  │  │   Router    │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                    Services Layer                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ Flood Engine │  │  Op Worker  │  │  Resources  │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  AI Services │  │   Caching   │  │  Scheduling │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                    Database Layer                           │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              PostgreSQL (asyncpg)                    │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Component Details

### 1. Bot Layer (`bot/`)

**Handlers** process Telegram updates (messages, callbacks).

Each handler module:
- Defines a `Router` instance
- Registers handlers with decorators
- Is included in the main `Dispatcher`

**Middlewares** process requests before/after handlers:

| Middleware | Purpose |
|------------|---------|
| `SubscriptionGateMiddleware` | Feature gating by plan |
| `UserActivityLogMiddleware` | Event logging |
| `LatencyMiddleware` | Performance monitoring |

**Utilities** provide shared helpers:
- `op_helpers.py` — retry logic, safe editing
- `subscription.py` — plan management
- `button_styles.py` — UI customization

### 2. Services Layer (`services/`)

**Core Engines** implement business logic:

| Engine | Responsibility |
|--------|----------------|
| `flood_engine` | FloodWait tracking, adaptive pacing |
| `op_worker` | Operation execution, retry, circuit breaker |
| `account_manager` | Session lifecycle, proxy binding |
| `resource_selector` | Account/proxy selection |
| `session_simulator` | Human-like behavior |

**Background Services** run as async tasks:

```python
asyncio.create_task(_resilient("service_name", service.run, pool, bot))
```

The `_resilient()` wrapper provides:
- Automatic restart on crash
- Staggered startup to avoid DB thundering herd
- 30-second cooldown between restarts

**AI Services** integrate with LLM providers:

| Service | Provider |
|---------|----------|
| `ai_providers` | OpenRouter, Groq, Gemini |
| `narrative_engine` | Content generation |
| `semantic_memory` | Vector search |
| `intent_planner` | Intent recognition |

### 3. Database Layer (`database/`)

**Connection Pool** (`asyncpg.Pool`):
- Created at startup
- Passed to all handlers/services via dependency injection
- Monitored by `pool_monitor` service

**Schema Migrations**:
- Applied automatically on startup (replayed in full every process start —
  no skip-already-applied logic, see `docs/DATABASE.md`)
- Files: `schema.sql`, `schema_v2.sql` ... `schema_v152.sql` (151 files as of
  2026-07-09 — uncontrolled growth already flagged, see
  `docs/SCHEMA_CONSOLIDATION_PLAN.md`)
- Idempotent (IF NOT EXISTS)

**Query Pattern**:
```python
# Always use parameterized queries
await pool.fetch("SELECT * FROM users WHERE id=$1", user_id)

# Bot queries auto-decrypt tokens
bot = await db.fetchrow_bot(pool, query, *args)
```

## Data Flow

### Handler Request Flow

```
Telegram Update
    ↓
Middleware (SubscriptionGate)
    ↓
Middleware (UserActivityLog)
    ↓
Middleware (Latency)
    ↓
Handler Function
    ↓
Service Call
    ↓
Database Query
    ↓
Response to Telegram
```

### Operation Execution Flow

```
User triggers bulk operation
    ↓
Handler validates & creates operation_queue entry
    ↓
op_worker picks up operation
    ↓
resource_selector selects accounts
    ↓
flood_engine checks cooldowns
    ↓
session_simulator adds delays
    ↓
Telethon executes action
    ↓
Result recorded to operation_audit
```

## Key Patterns

### 1. Resilient Background Services

```python
async def _resilient(name: str, fn, *args):
    """Auto-restart on crash with staggered startup."""
    while True:
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Service %s crashed: %s", name, e)
            await asyncio.sleep(30)
```

### 2. Circuit Breaker

Prevents cascade failures by pausing operations when consecutive errors exceed threshold.

### 3. Adaptive Pacing

Dynamically adjusts delays based on FloodWait signals:
- Baseline per action type
- Learned adjustments from floods
- Risk score multipliers
- Time-of-day factors

### 4. Token Encryption

Sensitive credentials are encrypted at rest:
```python
from services.token_vault import encrypt_token, decrypt_token

encrypted = encrypt_token("bot_token")  # ENC:<base64>
decrypted = decrypt_token(encrypted)    # plaintext
```

## Deployment

### Railway

- Auto-deploys from `claude/telegram-bot-services-xfAh6` branch
- PostgreSQL add-on
- Environment variables for configuration

### Docker

```bash
docker build -t infragram .
docker run -e MANAGER_BOT_TOKEN=... infragram
```

### Local Development

```bash
cp .env.example .env  # Fill credentials
pip install -e .
python -m tg-manager.main
```

## Monitoring

### Health Checks

- Pool monitor: checks connection pool every 5 minutes
- Session health monitor: validates sessions every 6 hours
- Account health: continuous scoring

### Metrics

- Latency middleware logs slow handlers
- Activity logger tracks all user events
- Circuit breaker logs automatic pauses

### Logs

Structured logging with correlation IDs:
```python
from services.logger import get_logger, set_correlation_id

log = get_logger(__name__)
set_correlation_id(user_id=12345, op_id="op_abc")
log.info("Processing", extra={"account_id": 42})
```
