import asyncpg
from config import DATABASE_URL


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=20)


# ── Managed bots ───────────────────────────────────────────────────────────

async def add_bot(pool: asyncpg.Pool, token: str, bot_id: int, username: str,
                  first_name: str, added_by: int) -> bool:
    """Return True if inserted, False if token already exists."""
    try:
        await pool.execute(
            """INSERT INTO managed_bots (token, bot_id, username, first_name, added_by)
               VALUES ($1, $2, $3, $4, $5)""",
            token, bot_id, username, first_name, added_by,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def get_bots(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY added_at DESC",
        added_by,
    )


async def get_bot(pool: asyncpg.Pool, bot_id: int, added_by: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, added_by,
    )


async def delete_bot(pool: asyncpg.Pool, bot_id: int, added_by: int) -> bool:
    result = await pool.execute(
        "UPDATE managed_bots SET is_active=FALSE WHERE bot_id=$1 AND added_by=$2",
        bot_id, added_by,
    )
    return result == "UPDATE 1"


# ── Audience ───────────────────────────────────────────────────────────────

async def upsert_users(pool: asyncpg.Pool, bot_id: int, users: list[dict]) -> int:
    """Insert or refresh last_seen for each user. Returns count of new rows."""
    if not users:
        return 0
    inserted = 0
    async with pool.acquire() as conn:
        for u in users:
            result = await conn.execute(
                """INSERT INTO bot_users (bot_id, user_id, username, first_name, last_name, language_code)
                   VALUES ($1, $2, $3, $4, $5, $6)
                   ON CONFLICT (bot_id, user_id) DO UPDATE SET
                       last_seen     = NOW(),
                       username      = EXCLUDED.username,
                       first_name    = EXCLUDED.first_name,
                       last_name     = EXCLUDED.last_name,
                       language_code = EXCLUDED.language_code""",
                bot_id,
                u["user_id"],
                u.get("username"),
                u.get("first_name"),
                u.get("last_name"),
                u.get("language_code"),
            )
            if result == "INSERT 1":
                inserted += 1
    return inserted


async def get_audience_count(pool: asyncpg.Pool, bot_id: int) -> int:
    return await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", bot_id
    )


async def get_audience_user_ids(pool: asyncpg.Pool, bot_id: int) -> list[int]:
    rows = await pool.fetch(
        "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", bot_id
    )
    return [r["user_id"] for r in rows]


async def compare_audiences(pool: asyncpg.Pool, bot_id_a: int, bot_id_b: int) -> dict:
    rows = await pool.fetch(
        """SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE
           INTERSECT
           SELECT user_id FROM bot_users WHERE bot_id=$2 AND is_active=TRUE""",
        bot_id_a, bot_id_b,
    )
    count_a = await get_audience_count(pool, bot_id_a)
    count_b = await get_audience_count(pool, bot_id_b)
    overlap = len(rows)
    return {
        "count_a": count_a,
        "count_b": count_b,
        "overlap": overlap,
        "overlap_pct_a": round(overlap / count_a * 100, 1) if count_a else 0,
        "overlap_pct_b": round(overlap / count_b * 100, 1) if count_b else 0,
    }


async def mark_user_inactive(pool: asyncpg.Pool, bot_id: int, user_id: int) -> None:
    await pool.execute(
        "UPDATE bot_users SET is_active=FALSE WHERE bot_id=$1 AND user_id=$2",
        bot_id, user_id,
    )


# ── Broadcasts ────────────────────────────────────────────────────────────

async def create_broadcast(pool: asyncpg.Pool, bot_id: int, message_text: str,
                            total: int, created_by: int) -> int:
    return await pool.fetchval(
        """INSERT INTO broadcasts (bot_id, message_text, total_users, status, created_by)
           VALUES ($1, $2, $3, 'pending', $4) RETURNING id""",
        bot_id, message_text, total, created_by,
    )


async def update_broadcast(pool: asyncpg.Pool, broadcast_id: int,
                            sent: int, failed: int, status: str) -> None:
    finished = "NOW()" if status in ("done", "cancelled") else "NULL"
    await pool.execute(
        f"""UPDATE broadcasts
            SET sent_count=$2, failed_count=$3, status=$4, finished_at={finished}
            WHERE id=$1""",
        broadcast_id, sent, failed, status,
    )


async def get_broadcast(pool: asyncpg.Pool, broadcast_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow("SELECT * FROM broadcasts WHERE id=$1", broadcast_id)


async def get_recent_broadcasts(pool: asyncpg.Pool, bot_id: int, limit: int = 5) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT $2",
        bot_id, limit,
    )
