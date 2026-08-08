# Services Module

Business logic engines and background services for Infragram.

## Core Engines

### Flood Engine (`flood_engine.py`)
Centralized FloodWait tracking and adaptive pacing.
- Per-account cooldown management
- Risk scoring and trust-based delays
- Circuit breaker for cascade failure prevention

### Op Worker (`op_worker.py`)
Background operation execution engine.
- Parallel operation processing
- Smart retry with Telegram-specific error handling
- Circuit breaker for automatic pausing

### Account Manager (`account_manager.py`)
Telethon session management.
- Phone/QR login flows
- Session creation and proxy binding
- Health monitoring

### Session Simulator (`session_simulator.py`)
Human-like behavior simulation.
- Beta-distributed delays
- Typing simulation
- Time-of-day factors

### Resource Selector (`resource_selector.py`)
Unified account and proxy selection.
- Flood-aware account ranking
- Trust score integration
- Cooldown respect

## Background Services

| Service | Description |
|---------|-------------|
| `scheduler.py` | Cron-like scheduled tasks |
| `auto_responder.py` | Automatic message responses |
| `relay.py` | Message relay between bots |
| `funnel_runner.py` | Sales funnel execution |
| `payment_checker.py` | Payment verification |
| `account_monitor.py` | Account health monitoring |
| `trust_engine.py` | Trust score calculation |
| `shadowban_monitor.py` | Shadowban detection |
| `behavioral_engine.py` | User behavior tracking |
| `account_warmer.py` | New account warming |
| `drift_detector.py` | Ecosystem drift detection |
| `anomaly_detector.py` | Anomaly detection |

## AI Services

| Service | Description |
|---------|-------------|
| `ai_providers.py` | Multi-provider AI selection |
| `narrative_engine.py` | Content generation |
| `semantic_memory.py` | Semantic search |
| `intent_planner.py` | Intent recognition |

## Infrastructure Services

| Service | Description |
|---------|-------------|
| `proxy_scraper.py` | Proxy collection |
| `infra_memory.py` | Infrastructure state |
| `infra_copilot.py` | Infrastructure advisor |
| `db_maintenance.py` | Database cleanup |

## Adding a New Service

1. Create a new file in `services/`
2. Implement a `run()` async function
3. Register in `main.py` with `_resilient()`

```python
# services/my_service.py
import asyncpg
from aiogram import Bot

async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    while True:
        # Your service logic
        await asyncio.sleep(60)
```

```python
# main.py
asyncio.create_task(_resilient("my_service", my_service.run, pool, bot))
```
