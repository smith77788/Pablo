# BotMother — Architecture

## Stack

| Component | Technology |
|-----------|-----------|
| Bot Framework | aiogram 3.13.1 + Pydantic v2 |
| Database | PostgreSQL via asyncpg (Railway) |
| Telegram API | Telethon (userbot) + Bot API |
| Deploy | Railway, Root Dir = `/tg-manager`, auto-deploy |
| Branch | `claude/telegram-bot-services-xfAh6` |

## File structure

```
tg-manager/
├── main.py                        # entry point, router + service registration
├── config.py                      # BOT_TOKEN, DB_URL, ADMIN_IDS, ENCRYPTION_KEY
├── database/
│   ├── db.py                      # 163+ functions, create_pool() auto-migrates schema_v*.sql
│   └── schema_v*.sql              # incremental migrations (current: v33)
├── bot/
│   ├── callbacks.py               # ALL CallbackData classes (46 prefixes)
│   ├── states.py                  # ALL FSMState classes
│   ├── keyboards.py               # shared keyboards
│   └── handlers/                  # 44+ handler files
└── services/                      # 12 background services
    ├── account_manager.py         # ALL Telethon ops (singleton pattern)
    ├── behavioral_engine.py       # behavioral scores (every 15 min)
    ├── session_simulator.py       # human-like delays (beta distribution)
    └── [10 more services]
```

## Key patterns

- **CallbackData**: Pydantic v2, Optional fields (never `str = ""`), all in `callbacks.py`
- **FSM states**: all in `states.py`
- **Subscription gate**: `require_plan()` + `locked_text()` + `subscription_locked_markup()`
- **Telethon ops**: always pass `_acc=acc` for device fingerprint isolation
- **Common helpers**: `op_helpers.py` — `_acc_label`, `_progress_bar`, `_format_duration`
- **SQL**: always parameterized `$1, $2` via asyncpg

## Database

- Auto-migration: `create_pool()` runs all `schema_v*.sql` files in version order
- Current version: v33
- Rule: new schema → new file `schema_v{N+1}.sql`
- 57+ tables: infrastructure, operations, visibility, behavioral, users, billing

## Background services (12 active)

scheduler, auto_responder, relay_service, funnel_runner, payment_checker,
ranking_checker, search_observer, account_monitor, trust_engine,
shadowban_monitor, op_worker, behavioral_engine

## Router registration order

More specific first; admin_handler last; relay_handler second-to-last.
44+ routers total — see CLAUDE.md section 6 for exact order.

## Anti-ban system

- 20 unique Android device profiles per account (schema_v23)
- `generate_device_fingerprint()` → random profile on creation
- `_make_client(session_str, _acc)` → uses account's saved profile
- `session_simulator.chaos_factor()` — 0.7–1.3 multiplier

## Reusable patterns

1. Op Queue (`operation_queue` table + `op_worker`) — all mass ops go here
2. Asset Templates (`asset_templates.py`) — reusable configs for bots/channels/groups/posts
3. Subscription Gate — uniform locking of features by plan tier
4. Behavioral Events — unified log for all user interactions with entities
5. Import from Telegram — accounts can sync existing channels/groups

## Known risks

- Router order mistakes cause silent handler conflicts (first registered wins)
- `str=""` in CallbackData breaks aiogram 3.13 deserialization
- Telethon without `_acc` → shared fingerprint → ban risk
- f-string SQL → injection risk (always use `$1, $2`)

## Deployment

- Platform: Railway, Branch: `claude/telegram-bot-services-xfAh6`
- Verify after deploy: `/version` in the bot
