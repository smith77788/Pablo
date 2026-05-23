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


# ── Audience stats ────────────────────────────────────────────────────────

async def get_audience_stats(pool: asyncpg.Pool, bot_id: int) -> dict:
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", bot_id
    )
    inactive = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=FALSE", bot_id
    )
    langs = await pool.fetch(
        """SELECT COALESCE(language_code, 'unknown') AS lang, COUNT(*) AS cnt
           FROM bot_users WHERE bot_id=$1 AND is_active=TRUE
           GROUP BY lang ORDER BY cnt DESC LIMIT 10""",
        bot_id,
    )
    return {
        "total": total or 0,
        "inactive": inactive or 0,
        "languages": [{"lang": r["lang"], "count": r["cnt"]} for r in langs],
    }


async def get_audience_full(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT user_id, username, first_name, last_name, language_code,
                  first_seen, last_seen, is_active
           FROM bot_users WHERE bot_id=$1 ORDER BY first_seen""",
        bot_id,
    )


# ── Message templates ─────────────────────────────────────────────────────

async def save_template(pool: asyncpg.Pool, owner_id: int, name: str, text: str) -> bool:
    try:
        await pool.execute(
            "INSERT INTO message_templates (owner_id, name, text) VALUES ($1,$2,$3)",
            owner_id, name, text,
        )
        return True
    except asyncpg.UniqueViolationError:
        return False


async def get_templates(pool: asyncpg.Pool, owner_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM message_templates WHERE owner_id=$1 ORDER BY created_at DESC",
        owner_id,
    )


async def get_template(pool: asyncpg.Pool, template_id: int, owner_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM message_templates WHERE id=$1 AND owner_id=$2",
        template_id, owner_id,
    )


async def delete_template(pool: asyncpg.Pool, template_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM message_templates WHERE id=$1 AND owner_id=$2",
        template_id, owner_id,
    )
    return result == "DELETE 1"


# ── Scheduled broadcasts ──────────────────────────────────────────────────

async def create_scheduled(pool: asyncpg.Pool, bot_id: int, text: str,
                             execute_at, created_by: int) -> int:
    return await pool.fetchval(
        """INSERT INTO scheduled_broadcasts (bot_id, message_text, execute_at, created_by)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        bot_id, text, execute_at, created_by,
    )


async def get_pending_scheduled(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT s.*, m.token FROM scheduled_broadcasts s
           JOIN managed_bots m ON m.bot_id=s.bot_id
           WHERE s.status='pending' AND s.execute_at <= NOW()""",
    )


async def mark_scheduled_done(pool: asyncpg.Pool, schedule_id: int) -> None:
    await pool.execute(
        "UPDATE scheduled_broadcasts SET status='done' WHERE id=$1", schedule_id
    )


async def cancel_scheduled(pool: asyncpg.Pool, schedule_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        """UPDATE scheduled_broadcasts SET status='cancelled'
           WHERE id=$1 AND created_by=$2 AND status='pending'""",
        schedule_id, owner_id,
    )
    return result == "UPDATE 1"


async def get_bot_schedules(pool: asyncpg.Pool, bot_id: int, limit: int = 10) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM scheduled_broadcasts WHERE bot_id=$1 ORDER BY execute_at DESC LIMIT $2",
        bot_id, limit,
    )


# ── Auto-replies ──────────────────────────────────────────────────────────

async def get_auto_replies(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM auto_replies WHERE bot_id=$1 ORDER BY id", bot_id)


async def get_active_auto_replies(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM auto_replies WHERE bot_id=$1 AND is_active=true ORDER BY id", bot_id
    )


async def add_auto_reply(pool: asyncpg.Pool, bot_id: int, trigger_type: str,
                          keyword: str | None, response_text: str) -> asyncpg.Record:
    return await pool.fetchrow(
        "INSERT INTO auto_replies(bot_id,trigger_type,keyword,response_text) VALUES($1,$2,$3,$4) RETURNING id",
        bot_id, trigger_type, keyword, response_text,
    )


async def toggle_auto_reply(pool: asyncpg.Pool, reply_id: int, bot_id: int) -> str:
    return await pool.execute(
        "UPDATE auto_replies SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2",
        reply_id, bot_id,
    )


async def delete_auto_reply(pool: asyncpg.Pool, reply_id: int, bot_id: int) -> str:
    return await pool.execute("DELETE FROM auto_replies WHERE id=$1 AND bot_id=$2", reply_id, bot_id)


# ── Update offsets ────────────────────────────────────────────────────────

async def get_update_offset(pool: asyncpg.Pool, bot_id: int) -> int:
    row = await pool.fetchrow(
        "SELECT last_update_id FROM bot_update_offsets WHERE bot_id=$1", bot_id
    )
    return row["last_update_id"] if row else 0


async def set_update_offset(pool: asyncpg.Pool, bot_id: int, offset: int) -> None:
    await pool.execute(
        "INSERT INTO bot_update_offsets(bot_id,last_update_id) VALUES($1,$2) "
        "ON CONFLICT(bot_id) DO UPDATE SET last_update_id=$2",
        bot_id, offset,
    )


async def get_bots_with_auto_replies(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT DISTINCT b.bot_id, b.token FROM managed_bots b "
        "JOIN auto_replies ar ON ar.bot_id=b.bot_id WHERE ar.is_active=true"
    )


async def get_bots_for_polling(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Return all active bots that have at least one auto-reply or active funnel."""
    return await pool.fetch(
        """SELECT DISTINCT b.bot_id, b.token FROM managed_bots b
           WHERE b.is_active=true AND (
               EXISTS (SELECT 1 FROM auto_replies ar WHERE ar.bot_id=b.bot_id AND ar.is_active=true)
               OR EXISTS (SELECT 1 FROM funnels f WHERE f.bot_id=b.bot_id AND f.is_active=true)
           )"""
    )


# ── Hermes Relay ───────────────────────────────────────────────────────────

async def enable_relay(pool: asyncpg.Pool, bot_id: int, enabled: bool) -> None:
    await pool.execute(
        "UPDATE managed_bots SET relay_enabled=$1 WHERE bot_id=$2", enabled, bot_id
    )


async def get_bots_with_relay(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT bot_id, token, added_by FROM managed_bots "
        "WHERE relay_enabled=true AND is_active=true"
    )


async def get_or_create_relay_session(pool: asyncpg.Pool, bot_id: int, user_id: int,
                                       username: str | None, first_name: str | None) -> int:
    row = await pool.fetchrow(
        "SELECT id FROM relay_sessions WHERE bot_id=$1 AND user_id=$2", bot_id, user_id
    )
    if row:
        await pool.execute(
            "UPDATE relay_sessions SET last_activity=now(), username=$3, first_name=$4, "
            "messages_count=messages_count+1 WHERE bot_id=$1 AND user_id=$2",
            bot_id, user_id, username, first_name,
        )
        return row["id"]
    row = await pool.fetchrow(
        "INSERT INTO relay_sessions(bot_id,user_id,username,first_name) "
        "VALUES($1,$2,$3,$4) RETURNING id",
        bot_id, user_id, username, first_name,
    )
    return row["id"]


async def save_relay_message(pool: asyncpg.Pool, session_id: int, direction: str,
                              text: str, forwarded_msg_id: int | None = None) -> None:
    await pool.execute(
        "INSERT INTO relay_messages(session_id,direction,text,forwarded_msg_id) "
        "VALUES($1,$2,$3,$4)",
        session_id, direction, text, forwarded_msg_id,
    )


async def find_session_by_forwarded_msg(pool: asyncpg.Pool,
                                         forwarded_msg_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """SELECT rs.bot_id, rs.user_id, mb.token
           FROM relay_messages rm
           JOIN relay_sessions rs ON rs.id = rm.session_id
           JOIN managed_bots mb ON mb.bot_id = rs.bot_id
           WHERE rm.forwarded_msg_id=$1""",
        forwarded_msg_id,
    )


async def get_relay_sessions(pool: asyncpg.Pool, bot_id: int,
                              limit: int = 5) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT rs.id, rs.user_id, rs.username, rs.first_name, rs.last_activity, rs.messages_count,
                  (SELECT text FROM relay_messages WHERE session_id=rs.id
                   ORDER BY created_at DESC LIMIT 1) as last_text
           FROM relay_sessions rs WHERE rs.bot_id=$1
           ORDER BY rs.last_activity DESC LIMIT $2""",
        bot_id, limit,
    )


async def get_relay_session_messages(pool: asyncpg.Pool, session_id: int,
                                      limit: int = 20) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT direction, text AS message_text, created_at
           FROM relay_messages WHERE session_id=$1
           ORDER BY created_at DESC LIMIT $2""",
        session_id, limit,
    )


async def close_relay_session(pool: asyncpg.Pool, session_id: int) -> None:
    await pool.execute("DELETE FROM relay_sessions WHERE id=$1", session_id)


# ── Funnels ────────────────────────────────────────────────────────────────

async def get_funnels(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM funnels WHERE bot_id=$1 ORDER BY id", bot_id)


async def get_active_funnels(pool: asyncpg.Pool, bot_id: int) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id)


async def create_funnel(pool: asyncpg.Pool, bot_id: int, name: str,
                        trigger_type: str, keyword: str | None = None) -> asyncpg.Record:
    return await pool.fetchrow(
        "INSERT INTO funnels(bot_id,name,trigger_type,keyword) VALUES($1,$2,$3,$4) RETURNING id",
        bot_id, name, trigger_type, keyword,
    )


async def delete_funnel(pool: asyncpg.Pool, funnel_id: int, bot_id: int) -> None:
    await pool.execute("DELETE FROM funnels WHERE id=$1 AND bot_id=$2", funnel_id, bot_id)


async def toggle_funnel(pool: asyncpg.Pool, funnel_id: int, bot_id: int) -> None:
    await pool.execute(
        "UPDATE funnels SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2", funnel_id, bot_id,
    )


async def get_funnel_steps(pool: asyncpg.Pool, funnel_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order", funnel_id,
    )


async def add_funnel_step(pool: asyncpg.Pool, funnel_id: int, step_order: int,
                          message_text: str, delay_minutes: int) -> None:
    await pool.execute(
        "INSERT INTO funnel_steps(funnel_id,step_order,message_text,delay_minutes) VALUES($1,$2,$3,$4)"
        " ON CONFLICT(funnel_id,step_order) DO UPDATE SET message_text=$3,delay_minutes=$4",
        funnel_id, step_order, message_text, delay_minutes,
    )


async def subscribe_to_funnel(pool: asyncpg.Pool, funnel_id: int, user_id: int) -> None:
    await pool.execute(
        "INSERT INTO funnel_subscriptions(funnel_id,user_id) VALUES($1,$2)"
        " ON CONFLICT(funnel_id,user_id) DO UPDATE SET current_step=0,completed=false,next_send_at=now()",
        funnel_id, user_id,
    )


async def get_due_funnel_steps(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Returns subscriptions where next step is due."""
    return await pool.fetch(
        """SELECT fs.id as sub_id, fs.funnel_id, fs.user_id, fs.current_step,
                  fst.message_text, fst.delay_minutes,
                  f.bot_id, mb.token,
                  (SELECT COUNT(*) FROM funnel_steps WHERE funnel_id=fs.funnel_id) as total_steps
           FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id AND f.is_active=true
           JOIN funnel_steps fst ON fst.funnel_id=fs.funnel_id AND fst.step_order=fs.current_step
           JOIN managed_bots mb ON mb.bot_id=f.bot_id
           WHERE fs.completed=false AND fs.next_send_at<=now()""",
    )


async def advance_funnel_step(pool: asyncpg.Pool, sub_id: int, next_step: int,
                               total_steps: int, delay_minutes: int) -> None:
    if next_step >= total_steps:
        await pool.execute(
            "UPDATE funnel_subscriptions SET completed=true WHERE id=$1", sub_id,
        )
    else:
        from datetime import datetime, timedelta
        next_at = datetime.utcnow() + timedelta(minutes=delay_minutes)
        await pool.execute(
            "UPDATE funnel_subscriptions SET current_step=$2, next_send_at=$3 WHERE id=$1",
            sub_id, next_step, next_at,
        )


async def get_bot_stats(pool: asyncpg.Pool, bot_id: int) -> dict:
    """Get aggregated statistics for a bot."""
    # Count relay sessions (users who contacted bot via relay)
    relay_sessions = await pool.fetchval(
        "SELECT COUNT(*) FROM relay_sessions WHERE bot_id=$1", bot_id
    )
    # Count relay messages in/out
    msg_in = await pool.fetchval(
        """SELECT COUNT(*) FROM relay_messages rm
           JOIN relay_sessions rs ON rs.id=rm.session_id
           WHERE rs.bot_id=$1 AND rm.direction='in'""", bot_id
    )
    msg_out = await pool.fetchval(
        """SELECT COUNT(*) FROM relay_messages rm
           JOIN relay_sessions rs ON rs.id=rm.session_id
           WHERE rs.bot_id=$1 AND rm.direction='out'""", bot_id
    )
    # Count active auto-replies
    active_replies = await pool.fetchval(
        "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=true", bot_id
    )
    # Count funnels
    active_funnels = await pool.fetchval(
        "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true", bot_id
    )
    # Count funnel subscriptions (unique users in funnel)
    funnel_users = await pool.fetchval(
        """SELECT COUNT(DISTINCT fs.user_id) FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id
           WHERE f.bot_id=$1""", bot_id
    )
    return {
        "relay_sessions": relay_sessions or 0,
        "msg_in": msg_in or 0,
        "msg_out": msg_out or 0,
        "active_replies": active_replies or 0,
        "active_funnels": active_funnels or 0,
        "funnel_users": funnel_users or 0,
    }


async def get_bots_with_funnels(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT DISTINCT b.bot_id, b.token, b.added_by FROM managed_bots b "
        "JOIN funnels f ON f.bot_id=b.bot_id WHERE f.is_active=true AND b.is_active=true"
    )
