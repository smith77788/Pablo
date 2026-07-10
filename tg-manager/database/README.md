# Database Module

PostgreSQL database management for Infragram.

## Structure

```
database/
├── __init__.py
└── db.py              # Connection pool, queries, schema migration
```

## Key Features

### Connection Pool

```python
from database.db import create_pool

pool = await create_pool()
```

The pool automatically applies all `schema*.sql` files on startup.

### Schema Migration

Schema files are applied in version order, in full, on every process start
(no skip-already-applied tracking — idempotent SQL is what makes repeated
application safe):
- `schema.sql` — base schema
- `schema_v2.sql` through `schema_v152.sql` — incremental migrations (151
  files as of 2026-07-09; growth already flagged as technical debt, see
  `docs/SCHEMA_CONSOLIDATION_PLAN.md` for a phased consolidation plan)

Each file is split into individual statements and executed idempotently.

### Query Helpers

```python
from database import db

# Get user info
user = await db.get_user_info(pool, user_id)

# Get bots for user
bots = await db.get_bots(pool, user_id)

# Add a new bot
success = await db.add_bot(pool, token, bot_id, username, first_name, added_by)
```

### Token Decryption

Bot tokens are stored encrypted. Query helpers auto-decrypt:

```python
# Token is automatically decrypted
bot = await db.fetchrow_bot(pool, "SELECT * FROM managed_bots WHERE bot_id=$1", bot_id)
print(bot["token"])  # plaintext
```

## Adding Migrations

1. Create `schema_v{N}.sql` in project root
2. Use `CREATE TABLE IF NOT EXISTS` for idempotency
3. Use `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for columns
4. Restart the bot — migrations apply automatically

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
