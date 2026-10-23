# Bot Module

Telegram bot handlers, middlewares, and utilities for Infragram.

## Structure

```
bot/
├── handlers/          # Command and callback query handlers
├── middlewares/       # Request processing middleware
├── utils/             # Helper functions and utilities
├── callbacks.py       # Callback data models
├── keyboards.py       # Inline keyboard builders
└── states.py          # FSM state definitions
```

## Handlers

Each handler file corresponds to a feature domain:

| Handler | Description |
|---------|-------------|
| `start.py` | `/start`, `/cancel`, main menu |
| `accounts.py` | Account management (login, list, delete) |
| `bots.py` | Bot management (add, list, configure) |
| `broadcast.py` | Broadcast messaging |
| `admin.py` | Admin commands and settings |
| `subscription.py` | Subscription and payment handling |

## Middlewares

| Middleware | Description |
|------------|-------------|
| `LatencyMiddleware` | Logs slow handlers (>500ms) |
| `UserActivityLogMiddleware` | Logs all user events to activity_log |
| `SubscriptionGateMiddleware` | Gates features by subscription plan |

## Utilities

| Utility | Description |
|---------|-------------|
| `op_helpers.py` | Retry logic, progress bars, safe editing |
| `subscription.py` | Plan checking and caching |
| `button_styles.py` | Button style patches |
| `event_status.py` | Event error tracking |

## Adding New Handlers

1. Create a new file in `bot/handlers/`
2. Define a `Router` instance
3. Register handlers with decorators
4. Import and include the router in `main.py`

```python
from aiogram import Router, F
from aiogram.filters import Command

router = Router()

@router.message(Command("mycommand"))
async def cmd_mycommand(message: Message, pool: asyncpg.Pool) -> None:
    await message.answer("Response")
```
