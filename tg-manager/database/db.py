import asyncpg
import glob
import json
import logging
import os
from config import DATABASE_URL

from services.logger import log_exc_swallow, timed

log = logging.getLogger(__name__)


async def create_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=20)
    async with pool.acquire() as conn:
        # Run all schema migration files in order — search both root and database/ subdir
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_dir = os.path.join(base_dir, "database")
        all_paths = (
            glob.glob(os.path.join(base_dir, "schema*.sql")) +
            glob.glob(os.path.join(db_dir, "schema*.sql"))
        )
        # Sort by version number, deduplicate by basename
        def _version_key(p: str) -> int:
            name = os.path.basename(p)
            if name == "schema.sql":
                return 0
            digits = "".join(filter(str.isdigit, name))
            return int(digits) if digits else 0
        all_paths.sort(key=_version_key)
        seen: set[str] = set()
        schema_files = []
        for p in all_paths:
            bn = os.path.basename(p)
            if bn not in seen:
                seen.add(bn)
                schema_files.append(p)
        for path in schema_files:
            with open(path) as f:
                sql = f.read().strip()
            if sql:
                try:
                    await conn.execute(sql)
                except Exception as exc:
                    log.warning("Schema %s failed (may already exist): %s", os.path.basename(path), exc)
    return pool


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
    except asyncpg.UniqueViolationError:
        return False
    # Referral activation: first bot creation counts as "active"
    existing_bots = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE", added_by
    ) or 0
    if existing_bots == 1:  # this was the first bot
        await mark_referral_activated(pool, added_by)
    return True


async def get_bots(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.*,
                  COALESCE(aud.cnt, 0) AS audience_count,
                  COALESCE(ar.ar_cnt, 0) AS active_replies_count
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt
               FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id = m.bot_id
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS ar_cnt
               FROM auto_replies WHERE is_active=TRUE GROUP BY bot_id
           ) ar ON ar.bot_id = m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           ORDER BY m.added_at DESC""",
        added_by,
    )


async def get_bot(pool: asyncpg.Pool, bot_id: int, added_by: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, added_by,
    )


async def delete_bot(pool: asyncpg.Pool, bot_id: int, added_by: int) -> bool:
    result = await pool.execute(
        "DELETE FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id, added_by,
    )
    return result == "DELETE 1"


async def save_bot_note(pool: asyncpg.Pool, bot_id: int, added_by: int, note: str) -> None:
    await pool.execute(
        "UPDATE managed_bots SET note=$3 WHERE bot_id=$1 AND added_by=$2",
        bot_id, added_by, note,
    )


# ── Audience ───────────────────────────────────────────────────────────────

async def upsert_users(pool: asyncpg.Pool, bot_id: int, users: list[dict]) -> int:
    """Insert or refresh last_seen for each user. Returns count of new rows."""
    if not users:
        return 0
    inserted = 0
    with timed(log, "upsert_users", extra={"bot_id": bot_id, "count": len(users)}):
        async with pool.acquire() as conn:
            for u in users:
                result = await conn.execute(
                    """INSERT INTO bot_users (bot_id, user_id, username, first_name, last_name, language_code, phone)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)
                       ON CONFLICT (bot_id, user_id) DO UPDATE SET
                           last_seen     = NOW(),
                           username      = EXCLUDED.username,
                           first_name    = EXCLUDED.first_name,
                           last_name     = EXCLUDED.last_name,
                           language_code = EXCLUDED.language_code,
                           phone         = COALESCE(EXCLUDED.phone, bot_users.phone)""",
                    bot_id,
                    u["user_id"],
                    u.get("username"),
                    u.get("first_name"),
                    u.get("last_name"),
                    u.get("language_code"),
                    u.get("phone"),
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
        "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE",
        bot_id,
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


async def get_user_by_id(pool: asyncpg.Pool, bot_id: int, user_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM bot_users WHERE bot_id=$1 AND user_id=$2", bot_id, user_id
    )


async def block_user(pool: asyncpg.Pool, bot_id: int, user_id: int, blocked: bool) -> None:
    await pool.execute(
        "UPDATE bot_users SET is_blocked=$3 WHERE bot_id=$1 AND user_id=$2",
        bot_id, user_id, blocked,
    )


async def mark_user_inactive(pool: asyncpg.Pool, bot_id: int, user_id: int) -> None:
    await pool.execute(
        "UPDATE bot_users SET is_active=FALSE WHERE bot_id=$1 AND user_id=$2",
        bot_id, user_id,
    )


# ── Broadcasts ────────────────────────────────────────────────────────────

async def create_broadcast(pool: asyncpg.Pool, bot_id: int, message_text: str,
                            total: int, created_by: int,
                            photo_file_id: str | None = None) -> int:
    return await pool.fetchval(
        """INSERT INTO broadcasts (bot_id, message_text, total_users, status, created_by, photo_file_id)
           VALUES ($1, $2, $3, 'pending', $4, $5) RETURNING id""",
        bot_id, message_text, total, created_by, photo_file_id,
    )


async def update_broadcast(pool: asyncpg.Pool, broadcast_id: int,
                            sent: int, failed: int, status: str) -> None:
    await pool.execute(
        """UPDATE broadcasts
           SET sent_count=$2, failed_count=$3, status=$4,
               finished_at=CASE WHEN $4 IN ('done','cancelled') THEN NOW() ELSE NULL END
           WHERE id=$1""",
        broadcast_id, sent, failed, status,
    )


async def get_broadcast(pool: asyncpg.Pool, broadcast_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow("SELECT * FROM broadcasts WHERE id=$1", broadcast_id)


async def get_recent_broadcasts(pool: asyncpg.Pool, bot_id: int, limit: int = 10) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT $2",
        bot_id, limit,
    )


async def get_broadcast_history(pool: asyncpg.Pool, bot_id: int, limit: int = 5) -> list[asyncpg.Record]:
    """Return last N broadcasts with stats for summary view."""
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
    joined_today = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '24 hours'",
        bot_id,
    )
    joined_week = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '7 days'",
        bot_id,
    )
    joined_month = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '30 days'",
        bot_id,
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
        "joined_today": joined_today or 0,
        "joined_week": joined_week or 0,
        "joined_month": joined_month or 0,
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
    """Return all active managed bots for polling (activity, deep links, swarm, A/B, etc.)."""
    return await pool.fetch(
        "SELECT bot_id, token FROM managed_bots WHERE is_active=true"
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


async def copy_funnels(pool: asyncpg.Pool, from_bot_id: int, to_bot_id: int) -> int:
    """Copy all funnels (with steps) from one bot to another. Returns count of copied funnels."""
    funnels = await pool.fetch("SELECT * FROM funnels WHERE bot_id=$1", from_bot_id)
    count = 0
    for f in funnels:
        new_funnel = await pool.fetchrow(
            "INSERT INTO funnels(bot_id, name, trigger_type, keyword) VALUES($1,$2,$3,$4) RETURNING id",
            to_bot_id, f["name"], f["trigger_type"], f["keyword"],
        )
        steps = await pool.fetch(
            "SELECT * FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order", f["id"]
        )
        for s in steps:
            await pool.execute(
                "INSERT INTO funnel_steps(funnel_id, step_order, message_text, delay_minutes) VALUES($1,$2,$3,$4)",
                new_funnel["id"], s["step_order"], s["message_text"], s["delay_minutes"],
            )
        count += 1
    return count


async def get_funnel_subscriber_ids(pool: asyncpg.Pool, funnel_id: int) -> list[int]:
    """Return user_ids of all active (not completed) funnel subscribers."""
    rows = await pool.fetch(
        "SELECT user_id FROM funnel_subscriptions WHERE funnel_id=$1", funnel_id
    )
    return [r["user_id"] for r in rows]


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
    with timed(log, "get_bot_stats", extra={"bot_id": bot_id}):
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
    # Funnel completion rate
    funnel_completed = await pool.fetchval(
        """SELECT COUNT(*) FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id
           WHERE f.bot_id=$1 AND fs.completed=true""", bot_id
    )
    funnel_total_subs = await pool.fetchval(
        """SELECT COUNT(*) FROM funnel_subscriptions fs
           JOIN funnels f ON f.id=fs.funnel_id
           WHERE f.bot_id=$1""", bot_id
    )
    # Relay sessions today (used last_activity since relay_sessions has no created_at)
    relay_today = await pool.fetchval(
        """SELECT COUNT(*) FROM relay_sessions
           WHERE bot_id=$1 AND last_activity >= NOW() - INTERVAL '24 hours'""", bot_id
    )
    # Audience growth
    aud_total = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", bot_id
    )
    aud_today = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '24 hours'",
        bot_id,
    )
    aud_week = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND first_seen >= NOW() - INTERVAL '7 days'",
        bot_id,
    )
    # Broadcast count
    broadcasts_total = await pool.fetchval(
        "SELECT COUNT(*) FROM broadcasts WHERE bot_id=$1", bot_id
    )
    broadcasts_sent = await pool.fetchval(
        "SELECT COALESCE(SUM(sent_count), 0) FROM broadcasts WHERE bot_id=$1", bot_id
    )
    return {
        "relay_sessions": relay_sessions or 0,
        "msg_in": msg_in or 0,
        "msg_out": msg_out or 0,
        "active_replies": active_replies or 0,
        "active_funnels": active_funnels or 0,
        "funnel_users": funnel_users or 0,
        "funnel_completed": funnel_completed or 0,
        "funnel_total_subs": funnel_total_subs or 0,
        "relay_today": relay_today or 0,
        "aud_total": aud_total or 0,
        "aud_today": aud_today or 0,
        "aud_week": aud_week or 0,
        "broadcasts_total": broadcasts_total or 0,
        "broadcasts_sent": broadcasts_sent or 0,
    }


async def get_bots_with_funnels(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT DISTINCT b.bot_id, b.token, b.added_by FROM managed_bots b "
        "JOIN funnels f ON f.bot_id=b.bot_id WHERE f.is_active=true AND b.is_active=true"
    )


async def update_bot_token(pool: asyncpg.Pool, bot_id: int, added_by: int,
                            new_token: str, new_bot_id: int,
                            username: str, first_name: str) -> None:
    await pool.execute(
        """UPDATE managed_bots
           SET token=$3, bot_id=$4, username=$5, first_name=$6
           WHERE bot_id=$1 AND added_by=$2""",
        bot_id, added_by, new_token, new_bot_id, username, first_name,
    )


async def get_audience_daily_growth(pool: asyncpg.Pool, bot_id: int, days: int = 7) -> list[dict]:
    """Returns list of {date, new_users} for the last N days."""
    rows = await pool.fetch(
        """SELECT DATE(first_seen AT TIME ZONE 'UTC') AS d, COUNT(*) AS cnt
           FROM bot_users
           WHERE bot_id=$1 AND first_seen >= NOW() - ($2 || ' days')::INTERVAL
           GROUP BY d ORDER BY d""",
        bot_id, str(days),
    )
    return [{"date": r["d"], "count": r["cnt"]} for r in rows]


async def get_audience_new_users(pool: asyncpg.Pool, bot_id: int, days: int) -> list[int]:
    """Return user_ids of active users who joined within the last N days."""
    rows = await pool.fetch(
        """SELECT user_id FROM bot_users
           WHERE bot_id=$1 AND is_active=TRUE
             AND first_seen >= NOW() - ($2 || ' days')::INTERVAL""",
        bot_id, str(days),
    )
    return [r["user_id"] for r in rows]


async def get_audience_by_language(pool: asyncpg.Pool, bot_id: int,
                                    lang_code: str) -> list[int]:
    """Return user_ids filtered by language_code."""
    rows = await pool.fetch(
        "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND language_code=$2",
        bot_id, lang_code,
    )
    return [r["user_id"] for r in rows]


async def get_audience_languages(pool: asyncpg.Pool, bot_id: int) -> list[dict]:
    """Return list of {lang, count} sorted by count desc."""
    rows = await pool.fetch(
        """SELECT COALESCE(language_code, 'unknown') AS lang, COUNT(*) AS cnt
           FROM bot_users WHERE bot_id=$1 AND is_active=TRUE
           GROUP BY lang ORDER BY cnt DESC LIMIT 10""",
        bot_id,
    )
    return [{"lang": r["lang"], "count": r["cnt"]} for r in rows]


async def copy_auto_replies(pool: asyncpg.Pool, from_bot_id: int, to_bot_id: int) -> int:
    """Copy all auto-replies from one bot to another. Returns count of copied rules."""
    rules = await pool.fetch(
        "SELECT trigger_type, keyword, response_text FROM auto_replies WHERE bot_id=$1",
        from_bot_id,
    )
    count = 0
    for r in rules:
        try:
            await pool.execute(
                """INSERT INTO auto_replies (bot_id, trigger_type, keyword, response_text)
                   VALUES ($1, $2, $3, $4)""",
                to_bot_id, r["trigger_type"], r["keyword"], r["response_text"],
            )
            count += 1
        except Exception as e:
            log.debug("copy_auto_replies: skip duplicate rule: %s", e)
    return count


# ── Swarm / Mode System ─────────────────────────────────────────────────

async def get_system_mode(pool: asyncpg.Pool) -> str:
    row = await pool.fetchrow("SELECT mode FROM system_mode WHERE id=1")
    return row["mode"] if row else "manual"

async def set_system_mode(pool: asyncpg.Pool, mode: str) -> None:
    await pool.execute(
        "UPDATE system_mode SET mode=$1, updated_at=now() WHERE id=1", mode
    )

async def set_bot_role(pool: asyncpg.Pool, bot_id: int, role: str,
                        cluster: str = "default") -> None:
    await pool.execute(
        "UPDATE managed_bots SET bot_role=$2, cluster=$3 WHERE bot_id=$1",
        bot_id, role, cluster,
    )

async def toggle_swarm(pool: asyncpg.Pool, bot_id: int, enabled: bool) -> None:
    await pool.execute(
        "UPDATE managed_bots SET swarm_enabled=$2 WHERE bot_id=$1", bot_id, enabled
    )

async def get_swarm_bots(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.*, bm.score, bm.ctr, bm.conversion_rate, bm.retention_d1
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND m.swarm_enabled=TRUE
           ORDER BY COALESCE(bm.score, 0) DESC""",
        added_by,
    )

async def update_bot_metrics(pool: asyncpg.Pool, bot_id: int,
                              ctr: float, conversion: float,
                              retention_d1: float, retention_d7: float) -> None:
    score = ctr * 0.3 + conversion * 0.4 + retention_d1 * 0.2 + retention_d7 * 0.1
    await pool.execute(
        """INSERT INTO bot_metrics (bot_id, ctr, conversion_rate, retention_d1, retention_d7, score)
           VALUES ($1, $2, $3, $4, $5, $6)
           ON CONFLICT (bot_id) DO UPDATE SET
               ctr=$2, conversion_rate=$3, retention_d1=$4,
               retention_d7=$5, score=$6, updated_at=now()""",
        bot_id, ctr, conversion, retention_d1, retention_d7, score,
    )


# ── CRM Tags ──────────────────────────────────────────────────────────────

async def add_user_tag(pool, bot_id: int, user_id: int, tag: str) -> bool:
    """Returns True if tag was new."""
    try:
        await pool.execute(
            "INSERT INTO user_tags(bot_id,user_id,tag) VALUES($1,$2,$3)",
            bot_id, user_id, tag,
        )
        return True
    except Exception:
        log.debug("add_user_tag: error for bot_id=%s user_id=%s tag=%s", bot_id, user_id, tag, exc_info=True)
        return False

async def remove_user_tag(pool, bot_id: int, user_id: int, tag: str) -> None:
    await pool.execute(
        "DELETE FROM user_tags WHERE bot_id=$1 AND user_id=$2 AND tag=$3",
        bot_id, user_id, tag,
    )

async def get_user_tags(pool, bot_id: int, user_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT tag FROM user_tags WHERE bot_id=$1 AND user_id=$2 ORDER BY tag",
        bot_id, user_id,
    )
    return [r["tag"] for r in rows]

async def get_tag_names(pool, bot_id: int) -> list[dict]:
    """All unique tags for this bot with counts."""
    rows = await pool.fetch(
        """SELECT tag, COUNT(*) as cnt
           FROM user_tags WHERE bot_id=$1
           GROUP BY tag ORDER BY cnt DESC LIMIT 30""",
        bot_id,
    )
    return [{"tag": r["tag"], "count": r["cnt"]} for r in rows]

async def get_users_by_tag(pool, bot_id: int, tag: str) -> list[int]:
    rows = await pool.fetch(
        "SELECT user_id FROM user_tags WHERE bot_id=$1 AND tag=$2",
        bot_id, tag,
    )
    return [r["user_id"] for r in rows]

# ── Automation Rules ───────────────────────────────────────────────────────

async def get_automation_rules(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM automation_rules WHERE bot_id=$1 ORDER BY id",
        bot_id,
    )

async def get_active_automation_rules(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM automation_rules WHERE bot_id=$1 AND is_active=TRUE",
        bot_id,
    )

async def add_automation_rule(pool, bot_id: int, name: str, trigger_type: str,
                               trigger_value, action_type: str, action_value: str) -> int:
    row = await pool.fetchrow(
        """INSERT INTO automation_rules(bot_id,name,trigger_type,trigger_value,action_type,action_value)
           VALUES($1,$2,$3,$4,$5,$6) RETURNING id""",
        bot_id, name, trigger_type, trigger_value, action_type, action_value,
    )
    return row["id"]

async def toggle_automation_rule(pool, rule_id: int, bot_id: int) -> None:
    await pool.execute(
        "UPDATE automation_rules SET is_active=NOT is_active WHERE id=$1 AND bot_id=$2",
        rule_id, bot_id,
    )

async def delete_automation_rule(pool, rule_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM automation_rules WHERE id=$1 AND bot_id=$2", rule_id, bot_id,
    )


# ── A/B Experiments ────────────────────────────────────────────────────────

async def get_experiments(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM experiments WHERE bot_id=$1 ORDER BY id DESC", bot_id
    )

async def get_experiment(pool, exp_id: int) -> asyncpg.Record | None:
    return await pool.fetchrow("SELECT * FROM experiments WHERE id=$1", exp_id)

async def get_experiment_variants(pool, exp_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1 ORDER BY id", exp_id
    )

async def create_experiment(pool, bot_id: int, name: str, exp_type: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO experiments(bot_id,name,experiment_type) VALUES($1,$2,$3) RETURNING id",
        bot_id, name, exp_type,
    )
    return row["id"]

async def add_experiment_variant(pool, exp_id: int, name: str, content: str, weight: int = 50) -> int:
    row = await pool.fetchrow(
        "INSERT INTO experiment_variants(experiment_id,name,content,weight) VALUES($1,$2,$3,$4) RETURNING id",
        exp_id, name, content, weight,
    )
    return row["id"]

async def set_experiment_status(pool, exp_id: int, status: str) -> None:
    await pool.execute(
        "UPDATE experiments SET status=$2 WHERE id=$1", exp_id, status
    )

async def get_active_experiment(pool, bot_id: int, exp_type: str = 'start_message'):
    return await pool.fetchrow(
        "SELECT * FROM experiments WHERE bot_id=$1 AND experiment_type=$2 AND status='active' LIMIT 1",
        bot_id, exp_type,
    )

async def assign_experiment_variant(pool, bot_id: int, user_id: int,
                                     exp_id: int) -> asyncpg.Record | None:
    """Assign user to variant using weighted random. Returns variant record."""
    import random
    existing = await pool.fetchrow(
        """SELECT ea.*, ev.content, ev.name as variant_name
           FROM experiment_assignments ea
           JOIN experiment_variants ev ON ev.id=ea.variant_id
           WHERE ea.bot_id=$1 AND ea.user_id=$2 AND ea.experiment_id=$3""",
        bot_id, user_id, exp_id,
    )
    if existing:
        return existing

    variants = await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1", exp_id
    )
    if not variants:
        return None

    # Weighted random selection
    total_weight = sum(v["weight"] for v in variants)
    r = random.randint(1, total_weight)
    cumulative = 0
    chosen = variants[0]
    for v in variants:
        cumulative += v["weight"]
        if r <= cumulative:
            chosen = v
            break

    try:
        await pool.execute(
            "INSERT INTO experiment_assignments(bot_id,user_id,experiment_id,variant_id) VALUES($1,$2,$3,$4)",
            bot_id, user_id, exp_id, chosen["id"],
        )
        await pool.execute(
            "UPDATE experiment_variants SET impressions=impressions+1 WHERE id=$1", chosen["id"]
        )
    except Exception as e:
        log.debug("assign_experiment_variant: skip (likely duplicate assignment): %s", e)
    return chosen

async def record_experiment_conversion(pool, bot_id: int, user_id: int, exp_id: int) -> None:
    assignment = await pool.fetchrow(
        "SELECT * FROM experiment_assignments WHERE bot_id=$1 AND user_id=$2 AND experiment_id=$3 AND converted=FALSE",
        bot_id, user_id, exp_id,
    )
    if assignment:
        await pool.execute(
            "UPDATE experiment_assignments SET converted=TRUE WHERE id=$1", assignment["id"]
        )
        await pool.execute(
            "UPDATE experiment_variants SET conversions=conversions+1 WHERE id=$1",
            assignment["variant_id"],
        )

async def check_experiment_winner(pool, exp_id: int) -> int | None:
    """If any variant has >= min_sample_size and highest CTR, return its id. Else None."""
    exp = await pool.fetchrow("SELECT * FROM experiments WHERE id=$1", exp_id)
    if not exp or exp["status"] != "active":
        return None
    variants = await pool.fetch(
        "SELECT * FROM experiment_variants WHERE experiment_id=$1", exp_id
    )
    candidates = [v for v in variants if v["impressions"] >= exp["min_sample_size"]]
    if not candidates:
        return None
    best = max(candidates, key=lambda v: v["conversions"] / v["impressions"] if v["impressions"] else 0)
    ctr = best["conversions"] / best["impressions"] if best["impressions"] else 0
    if ctr > 0:
        await pool.execute(
            "UPDATE experiments SET status='completed', winner_variant_id=$2 WHERE id=$1",
            exp_id, best["id"],
        )
        return best["id"]
    return None

async def delete_experiment(pool, exp_id: int, bot_id: int) -> None:
    await pool.execute("DELETE FROM experiments WHERE id=$1 AND bot_id=$2", exp_id, bot_id)


# ── Routing Engine ─────────────────────────────────────────────────────────

async def get_best_conversion_bot(pool, cluster: str, exclude_bot_id: int) -> asyncpg.Record | None:
    """Get highest-scoring conversion/retention bot in the same cluster."""
    return await pool.fetchrow(
        """SELECT m.bot_id, m.token, m.username, m.first_name,
                  COALESCE(bm.score, 0) as score
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.swarm_enabled=TRUE
             AND m.is_active=TRUE
             AND m.bot_role IN ('conversion', 'retention', 'general')
             AND m.cluster=$1
             AND m.bot_id != $2
           ORDER BY COALESCE(bm.score, 0) DESC
           LIMIT 1""",
        cluster, exclude_bot_id,
    )

async def log_routing_decision(pool, from_bot_id: int, to_bot_id, user_id: int,
                                 decision: str, mode: str,
                                 score_from: float = 0, score_to: float = 0) -> None:
    await pool.execute(
        """INSERT INTO routing_log(from_bot_id,to_bot_id,user_id,decision,system_mode,score_from,score_to)
           VALUES($1,$2,$3,$4,$5,$6,$7)""",
        from_bot_id, to_bot_id, user_id, decision, mode, score_from, score_to,
    )

async def get_routing_stats(pool, bot_id: int, days: int = 7) -> dict:
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM routing_log WHERE from_bot_id=$1 AND created_at >= NOW()-($2||' days')::INTERVAL",
        bot_id, str(days),
    )
    routed = await pool.fetchval(
        "SELECT COUNT(*) FROM routing_log WHERE from_bot_id=$1 AND decision='routed' AND created_at >= NOW()-($2||' days')::INTERVAL",
        bot_id, str(days),
    )
    return {"total": total or 0, "routed": routed or 0, "kept": (total or 0) - (routed or 0)}

async def get_mode_routing_config(mode: str) -> dict:
    """Returns routing config based on system mode."""
    configs = {
        "manual": {"routing_enabled": False, "min_score_threshold": 0.0, "routing_probability": 0.0},
        "assisted": {"routing_enabled": False, "min_score_threshold": 0.3, "routing_probability": 0.0},
        "autopilot": {"routing_enabled": True, "min_score_threshold": 0.3, "routing_probability": 0.5},
        "growth": {"routing_enabled": True, "min_score_threshold": 0.2, "routing_probability": 0.8},
        "experiment": {"routing_enabled": True, "min_score_threshold": 0.1, "routing_probability": 1.0},
        "stability": {"routing_enabled": False, "min_score_threshold": 0.5, "routing_probability": 0.0},
    }
    return configs.get(mode, configs["manual"])


# ── Deep Links ──────────────────────────────────────────────────────────────

async def create_deep_link(pool, bot_id: int, name: str, start_param: str) -> int:
    row = await pool.fetchrow(
        "INSERT INTO bot_deep_links(bot_id,name,start_param) VALUES($1,$2,$3) RETURNING id",
        bot_id, name, start_param,
    )
    return row["id"]

async def get_deep_links(pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT * FROM bot_deep_links WHERE bot_id=$1 ORDER BY click_count DESC",
        bot_id,
    )

async def get_deep_link_by_param(pool, bot_id: int, param: str):
    return await pool.fetchrow(
        "SELECT * FROM bot_deep_links WHERE bot_id=$1 AND start_param=$2",
        bot_id, param,
    )

async def record_deep_link_visit(pool, bot_id: int, param: str, user_id: int) -> int | None:
    """Increments click_count, increments unique_users if first visit. Returns link_id or None."""
    link = await pool.fetchrow(
        "SELECT id FROM bot_deep_links WHERE bot_id=$1 AND start_param=$2", bot_id, param
    )
    if not link:
        return None
    link_id = link["id"]
    # Always count the click
    await pool.execute(
        "UPDATE bot_deep_links SET click_count=click_count+1 WHERE id=$1", link_id
    )
    # Track unique: try inserting into visits table
    try:
        result = await pool.execute(
            "INSERT INTO deep_link_visits(link_id, user_id) VALUES($1, $2)",
            link_id, user_id,
        )
        if result == "INSERT 0 1":
            # New unique visit
            await pool.execute(
                "UPDATE bot_deep_links SET unique_users=unique_users+1 WHERE id=$1", link_id
            )
    except Exception as e:
        log.debug("record_deep_link_visit: skip duplicate visit: %s", e)
    return link_id

async def delete_deep_link(pool, link_id: int, bot_id: int) -> None:
    await pool.execute(
        "DELETE FROM bot_deep_links WHERE id=$1 AND bot_id=$2", link_id, bot_id
    )

async def record_referral(pool, bot_id: int, referrer_user_id: int,
                           referred_user_id: int, deep_link_id: int | None = None) -> bool:
    """Returns True if referral was new."""
    try:
        await pool.execute(
            """INSERT INTO referrals(bot_id,referrer_user_id,referred_user_id,deep_link_id)
               VALUES($1,$2,$3,$4) ON CONFLICT (bot_id, referred_user_id) DO NOTHING""",
            bot_id, referrer_user_id, referred_user_id, deep_link_id,
        )
        return True
    except Exception as e:
        log.debug("record_referral: skip (likely duplicate): %s", e)
        return False

async def get_referral_leaderboard(pool, bot_id: int, limit: int = 10) -> list:
    return await pool.fetch(
        """SELECT referrer_user_id, COUNT(*) as referral_count
           FROM referrals WHERE bot_id=$1
           GROUP BY referrer_user_id ORDER BY referral_count DESC LIMIT $2""",
        bot_id, limit,
    )

async def get_referral_total(pool, bot_id: int) -> int:
    return await pool.fetchval(
        "SELECT COUNT(DISTINCT referred_user_id) FROM referrals WHERE bot_id=$1", bot_id
    ) or 0


# ── User Activity ──────────────────────────────────────────────────────────

async def upsert_user_activity(pool, bot_id: int, user_id: int) -> bool:
    """Returns True if this is the user's first message (new user)."""
    result = await pool.execute(
        """INSERT INTO user_activity(bot_id, user_id, message_count, last_seen, first_seen)
           VALUES($1, $2, 1, now(), now())
           ON CONFLICT (bot_id, user_id) DO UPDATE
           SET message_count = user_activity.message_count + 1,
               last_seen = now()""",
        bot_id, user_id,
    )
    return result == "INSERT 0 1"

async def get_activity_segments(pool, bot_id: int) -> dict:
    """Returns counts: hot(<1d), warm(1-7d), cold(7-30d), lost(30d+)."""
    rows = await pool.fetch(
        """SELECT
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '1 day')   AS hot,
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '7 days'
                                AND last_seen <  now() - INTERVAL '1 day')   AS warm,
             COUNT(*) FILTER (WHERE last_seen >= now() - INTERVAL '30 days'
                                AND last_seen <  now() - INTERVAL '7 days')  AS cold,
             COUNT(*) FILTER (WHERE last_seen <  now() - INTERVAL '30 days') AS lost,
             COUNT(*) AS total
           FROM user_activity WHERE bot_id=$1""",
        bot_id,
    )
    row = rows[0] if rows else {}
    return {
        "hot":   int(row.get("hot",  0) or 0),
        "warm":  int(row.get("warm", 0) or 0),
        "cold":  int(row.get("cold", 0) or 0),
        "lost":  int(row.get("lost", 0) or 0),
        "total": int(row.get("total",0) or 0),
    }

async def get_inactive_user_ids(pool, bot_id: int, min_days: int,
                                  max_days: int | None = None) -> list[int]:
    """Users not seen for min_days to max_days (None = no upper limit)."""
    if max_days is None:
        rows = await pool.fetch(
            """SELECT user_id FROM user_activity
               WHERE bot_id=$1 AND last_seen < now() - ($2 || ' days')::INTERVAL""",
            bot_id, str(min_days),
        )
    else:
        rows = await pool.fetch(
            """SELECT user_id FROM user_activity
               WHERE bot_id=$1
                 AND last_seen < now() - ($2 || ' days')::INTERVAL
                 AND last_seen >= now() - ($3 || ' days')::INTERVAL""",
            bot_id, str(min_days), str(max_days),
        )
    return [r["user_id"] for r in rows]

async def get_activity_heatmap(pool, bot_id: int, days: int = 7) -> list[dict]:
    """Message count per hour-of-day over last N days."""
    rows = await pool.fetch(
        """SELECT EXTRACT(HOUR FROM last_seen)::int AS hour, COUNT(*) AS cnt
           FROM user_activity
           WHERE bot_id=$1 AND last_seen >= now() - ($2 || ' days')::INTERVAL
           GROUP BY hour ORDER BY hour""",
        bot_id, str(days),
    )
    return [{"hour": r["hour"], "count": int(r["cnt"])} for r in rows]

async def get_top_active_users(pool, bot_id: int, limit: int = 10) -> list:
    return await pool.fetch(
        """SELECT user_id, message_count, last_seen
           FROM user_activity WHERE bot_id=$1
           ORDER BY message_count DESC LIMIT $2""",
        bot_id, limit,
    )

async def autotag_by_activity(pool, bot_id: int) -> dict:
    """Auto-tags users as activity:hot/warm/cold/lost. Returns counts."""
    segs = await get_activity_segments(pool, bot_id)

    async def _tag_segment(user_ids, tag):
        for uid in user_ids:
            try:
                await pool.execute(
                    "DELETE FROM user_tags WHERE bot_id=$1 AND user_id=$2 AND tag LIKE 'activity:%'",
                    bot_id, uid,
                )
                await pool.execute(
                    "INSERT INTO user_tags(bot_id,user_id,tag) VALUES($1,$2,$3) ON CONFLICT (bot_id,user_id,tag) DO NOTHING",
                    bot_id, uid, tag,
                )
            except Exception as e:
                log.debug("autotag_by_activity: skip duplicate tag: %s", e)

    hot_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '1 day'", bot_id
    )
    warm_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '7 days' AND last_seen < now() - INTERVAL '1 day'", bot_id
    )
    cold_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen >= now() - INTERVAL '30 days' AND last_seen < now() - INTERVAL '7 days'", bot_id
    )
    lost_ids = await pool.fetch(
        "SELECT user_id FROM user_activity WHERE bot_id=$1 AND last_seen < now() - INTERVAL '30 days'", bot_id
    )
    await _tag_segment([r["user_id"] for r in hot_ids], "activity:hot")
    await _tag_segment([r["user_id"] for r in warm_ids], "activity:warm")
    await _tag_segment([r["user_id"] for r in cold_ids], "activity:cold")
    await _tag_segment([r["user_id"] for r in lost_ids], "activity:lost")
    return segs

# ── Keyword Analytics ──────────────────────────────────────────────────────

async def record_message_keywords(pool, bot_id: int, text: str) -> None:
    import re
    words = list(set(re.findall(r'[а-яёА-ЯЁa-zA-Z]{3,}', text.lower())))[:10]
    for word in words:
        try:
            await pool.execute(
                """INSERT INTO keyword_stats(bot_id, keyword, count, last_seen)
                   VALUES($1, $2, 1, now())
                   ON CONFLICT (bot_id, keyword) DO UPDATE
                   SET count = keyword_stats.count + 1, last_seen = now()""",
                bot_id, word,
            )
        except Exception:
            log.debug("record_message_keywords: error recording keyword=%s bot_id=%s", word, bot_id, exc_info=True)

async def get_top_keywords(pool, bot_id: int, limit: int = 20) -> list:
    return await pool.fetch(
        "SELECT keyword, count FROM keyword_stats WHERE bot_id=$1 ORDER BY count DESC LIMIT $2",
        bot_id, limit,
    )

async def get_keyword_stats_summary(pool, bot_id: int) -> dict:
    total_keywords = await pool.fetchval(
        "SELECT COUNT(*) FROM keyword_stats WHERE bot_id=$1", bot_id
    ) or 0
    total_messages = await pool.fetchval(
        "SELECT SUM(count) FROM keyword_stats WHERE bot_id=$1", bot_id
    ) or 0
    return {"total_keywords": int(total_keywords), "total_messages": int(total_messages)}


# ── Network Management ──────────────────────────────────────────────────────

async def get_network_overview(pool: asyncpg.Pool, added_by: int) -> dict:
    """Aggregate stats across all active bots for a user."""
    meta = await pool.fetchrow(
        """SELECT COUNT(*) as total_bots,
                  SUM(CASE WHEN swarm_enabled THEN 1 ELSE 0 END) as swarm_bots,
                  COUNT(DISTINCT COALESCE(cluster,'default')) as clusters
           FROM managed_bots WHERE added_by=$1 AND is_active=TRUE""",
        added_by,
    )
    total_users = await pool.fetchval(
        """SELECT COUNT(*) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""", added_by,
    ) or 0
    unique_users = await pool.fetchval(
        """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""", added_by,
    ) or 0
    total_sent = await pool.fetchval(
        """SELECT COALESCE(SUM(bc.sent_count),0) FROM broadcasts bc
           JOIN managed_bots m ON m.bot_id=bc.bot_id WHERE m.added_by=$1""", added_by,
    ) or 0
    avg_score = await pool.fetchval(
        """SELECT AVG(bm.score) FROM bot_metrics bm
           JOIN managed_bots m ON m.bot_id=bm.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE""", added_by,
    ) or 0
    return {
        "total_bots": int(meta["total_bots"] or 0),
        "swarm_bots": int(meta["swarm_bots"] or 0),
        "clusters": int(meta["clusters"] or 0),
        "total_users": int(total_users),
        "unique_users": int(unique_users),
        "total_sent": int(total_sent),
        "avg_score": float(avg_score),
    }


async def get_cluster_list(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    """Return list of clusters with stats."""
    rows = await pool.fetch(
        """SELECT COALESCE(m.cluster,'default') as cluster,
                  COUNT(*) as bot_count,
                  SUM(CASE WHEN m.swarm_enabled THEN 1 ELSE 0 END) as swarm_count,
                  COALESCE(SUM(aud.cnt),0) as total_audience
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users
               WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           GROUP BY COALESCE(m.cluster,'default')
           ORDER BY total_audience DESC""",
        added_by,
    )
    return [dict(r) for r in rows]


async def get_bots_in_cluster(pool: asyncpg.Pool, added_by: int, cluster: str) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.*, COALESCE(aud.cnt,0) as audience_count, COALESCE(bm.score,0) as score
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND COALESCE(m.cluster,'default')=$2
           ORDER BY COALESCE(bm.score,0) DESC""",
        added_by, cluster,
    )


async def set_bot_cluster_name(pool: asyncpg.Pool, bot_id: int, added_by: int, cluster: str) -> None:
    await pool.execute(
        "UPDATE managed_bots SET cluster=$3 WHERE bot_id=$1 AND added_by=$2",
        bot_id, added_by, cluster,
    )


async def bulk_set_swarm(pool: asyncpg.Pool, added_by: int, cluster: str, enabled: bool) -> int:
    result = await pool.execute(
        """UPDATE managed_bots SET swarm_enabled=$3
           WHERE added_by=$1 AND COALESCE(cluster,'default')=$2 AND is_active=TRUE""",
        added_by, cluster, enabled,
    )
    return int(result.split()[-1])


async def bulk_set_role(pool: asyncpg.Pool, added_by: int, cluster: str, role: str) -> int:
    result = await pool.execute(
        """UPDATE managed_bots SET bot_role=$3
           WHERE added_by=$1 AND COALESCE(cluster,'default')=$2 AND is_active=TRUE""",
        added_by, cluster, role,
    )
    return int(result.split()[-1])


async def get_routing_weights_for_user(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.cluster,
                  m.bot_role, COALESCE(rw.weight, 1.0) as weight,
                  COALESCE(bm.score,0) as score
           FROM managed_bots m
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE AND m.swarm_enabled=TRUE
           ORDER BY m.cluster, weight DESC""",
        added_by,
    )


async def set_routing_weight(pool: asyncpg.Pool, bot_id: int, weight: float) -> None:
    await pool.execute(
        """INSERT INTO bot_routing_weights(bot_id, weight)
           VALUES($1,$2) ON CONFLICT(bot_id) DO UPDATE SET weight=$2, updated_at=NOW()""",
        bot_id, weight,
    )


async def reset_routing_weights(pool: asyncpg.Pool, added_by: int) -> None:
    await pool.execute(
        """DELETE FROM bot_routing_weights
           WHERE bot_id IN (SELECT bot_id FROM managed_bots WHERE added_by=$1)""",
        added_by,
    )


async def get_bot_ranking(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.cluster,
                  m.bot_role, m.swarm_enabled,
                  COALESCE(aud.cnt,0) as audience,
                  COALESCE(bm.score,0) as score,
                  COALESCE(bm.ctr,0) as ctr,
                  COALESCE(bm.conversion_rate,0) as conversion_rate,
                  COALESCE(rw.weight,1.0) as weight
           FROM managed_bots m
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           ORDER BY COALESCE(aud.cnt,0) DESC""",
        added_by,
    )


async def get_network_health(pool: asyncpg.Pool, added_by: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """SELECT m.bot_id, m.username, m.first_name, m.token, m.swarm_enabled, m.cluster,
                  COALESCE(o.last_update_id,0) as last_update_id,
                  COALESCE(aud.cnt,0) as audience
           FROM managed_bots m
           LEFT JOIN bot_update_offsets o ON o.bot_id=m.bot_id
           LEFT JOIN (
               SELECT bot_id, COUNT(*) AS cnt FROM bot_users WHERE is_active=TRUE GROUP BY bot_id
           ) aud ON aud.bot_id=m.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
           ORDER BY aud.cnt DESC""",
        added_by,
    )


async def get_unique_network_users(pool: asyncpg.Pool, added_by: int) -> list[dict]:
    """One (user_id, bot_id, token) per unique user. Picks most recently active bot."""
    rows = await pool.fetch(
        """SELECT DISTINCT ON (bu.user_id)
               bu.user_id, bu.bot_id, m.token
           FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND m.is_active=TRUE
             AND bu.is_active=TRUE AND bu.is_blocked=FALSE
           ORDER BY bu.user_id, bu.last_seen DESC""",
        added_by,
    )
    return [dict(r) for r in rows]


async def get_bot_overlap_stats(pool: asyncpg.Pool, added_by: int) -> dict:
    total_entries = await pool.fetchval(
        """SELECT COUNT(*) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""", added_by,
    ) or 0
    unique_users = await pool.fetchval(
        """SELECT COUNT(DISTINCT bu.user_id) FROM bot_users bu
           JOIN managed_bots m ON m.bot_id=bu.bot_id
           WHERE m.added_by=$1 AND bu.is_active=TRUE""", added_by,
    ) or 0
    multi_bot = await pool.fetchval(
        """SELECT COUNT(*) FROM (
               SELECT bu.user_id FROM bot_users bu
               JOIN managed_bots m ON m.bot_id=bu.bot_id
               WHERE m.added_by=$1 AND bu.is_active=TRUE
               GROUP BY bu.user_id HAVING COUNT(DISTINCT bu.bot_id) > 1
           ) sub""", added_by,
    ) or 0
    return {
        "total_entries": int(total_entries),
        "unique_users": int(unique_users),
        "multi_bot_users": int(multi_bot),
        "overlap_pct": round(int(multi_bot) / int(unique_users) * 100, 1) if unique_users else 0,
    }


async def clone_bot_settings(pool: asyncpg.Pool, src_id: int, dst_id: int) -> dict:
    """Clone auto-replies, funnels (+steps), automation rules from src to dst."""
    counts = {"auto_replies": 0, "funnels": 0, "automation_rules": 0}
    replies = await pool.fetch(
        "SELECT trigger_type,keyword,response_text FROM auto_replies WHERE bot_id=$1", src_id,
    )
    for r in replies:
        try:
            await pool.execute(
                "INSERT INTO auto_replies(bot_id,trigger_type,keyword,response_text) VALUES($1,$2,$3,$4)",
                dst_id, r["trigger_type"], r["keyword"], r["response_text"],
            )
            counts["auto_replies"] += 1
        except Exception:
            log.debug("clone_bot_settings: error copying auto_reply for dst_id=%s", dst_id, exc_info=True)
    funnels_src = await pool.fetch(
        "SELECT id,name,trigger_type,keyword FROM funnels WHERE bot_id=$1", src_id,
    )
    for fn in funnels_src:
        try:
            new_fn = await pool.fetchrow(
                "INSERT INTO funnels(bot_id,name,trigger_type,keyword) VALUES($1,$2,$3,$4) RETURNING id",
                dst_id, fn["name"], fn["trigger_type"], fn["keyword"],
            )
            if new_fn:
                steps = await pool.fetch(
                    "SELECT step_order,message_text,delay_minutes FROM funnel_steps WHERE funnel_id=$1 ORDER BY step_order",
                    fn["id"],
                )
                for s in steps:
                    await pool.execute(
                        "INSERT INTO funnel_steps(funnel_id,step_order,message_text,delay_minutes) VALUES($1,$2,$3,$4)",
                        new_fn["id"], s["step_order"], s["message_text"], s["delay_minutes"],
                    )
                counts["funnels"] += 1
        except Exception:
            log.debug("clone_bot_settings: error copying funnel src=%s dst=%s", fn["id"], dst_id, exc_info=True)
    rules = await pool.fetch(
        "SELECT name,trigger_type,trigger_value,action_type,action_value FROM automation_rules WHERE bot_id=$1",
        src_id,
    )
    for r in rules:
        try:
            await pool.execute(
                """INSERT INTO automation_rules(bot_id,name,trigger_type,trigger_value,action_type,action_value)
                   VALUES($1,$2,$3,$4,$5,$6)""",
                dst_id, r["name"], r["trigger_type"], r["trigger_value"],
                r["action_type"], r["action_value"],
            )
            counts["automation_rules"] += 1
        except Exception:
            log.debug("clone_bot_settings: error copying automation_rule dst=%s", dst_id, exc_info=True)
    return counts


async def get_weighted_routing_target(pool: asyncpg.Pool, cluster: str,
                                       exclude_bot_id: int) -> asyncpg.Record | None:
    """Weighted random selection of routing target bot."""
    import random as _random
    candidates = await pool.fetch(
        """SELECT m.bot_id, m.token, m.username, m.first_name,
                  COALESCE(bm.score,0) as score,
                  COALESCE(rw.weight,1.0) as weight
           FROM managed_bots m
           LEFT JOIN bot_metrics bm ON bm.bot_id=m.bot_id
           LEFT JOIN bot_routing_weights rw ON rw.bot_id=m.bot_id
           WHERE m.swarm_enabled=TRUE AND m.is_active=TRUE
             AND m.bot_role IN ('conversion','retention','general')
             AND m.cluster=$1 AND m.bot_id!=$2""",
        cluster, exclude_bot_id,
    )
    if not candidates:
        return None
    total = sum(float(c["weight"]) for c in candidates)
    if total <= 0:
        return candidates[0]
    r = _random.random() * total
    cum = 0.0
    for c in candidates:
        cum += float(c["weight"])
        if r <= cum:
            return c
    return candidates[-1]


# ── Telegram user accounts ──────────────────────────────────────────────────

async def get_tg_accounts(pool: asyncpg.Pool, owner_id: int, status_filter: str | None = None) -> list:
    base = (
        "SELECT id, phone, tg_user_id, first_name, username, added_at, is_active, "
        "COALESCE(acc_status, 'active') AS acc_status, status_checked_at, status_reason "
        "FROM tg_accounts WHERE owner_id=$1"
    )
    if status_filter and status_filter != "all":
        return await pool.fetch(base + " AND COALESCE(acc_status,'active')=$2 ORDER BY added_at DESC", owner_id, status_filter)
    return await pool.fetch(base + " ORDER BY added_at DESC", owner_id)


async def update_acc_status(pool: asyncpg.Pool, acc_id: int, status: str, reason: str = "") -> None:
    await pool.execute(
        "UPDATE tg_accounts SET acc_status=$1, status_reason=$2, status_checked_at=now() WHERE id=$3",
        status, reason, acc_id,
    )


async def get_tg_account(pool: asyncpg.Pool, acc_id: int, owner_id: int):
    return await pool.fetchrow(
        """SELECT a.*, p.proxy_url, p.label AS proxy_label
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE
           WHERE a.id=$1 AND a.owner_id=$2""",
        acc_id, owner_id,
    )


async def get_account_for_telethon(pool, acc_id: int, owner_id: int | None = None):
    """Fetch account dict with device fingerprint + proxy_url for _make_client."""
    if owner_id is not None:
        return await pool.fetchrow(
            """SELECT a.id, a.session_str, a.phone, a.first_name,
                      a.device_model, a.system_version, a.app_version,
                      p.proxy_url
               FROM tg_accounts a
               LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
               WHERE a.id=$1 AND a.owner_id=$2""",
            acc_id, owner_id,
        )
    return await pool.fetchrow(
        """SELECT a.id, a.session_str, a.phone, a.first_name,
                  a.device_model, a.system_version, a.app_version,
                  p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.id=$1""",
        acc_id,
    )


async def add_tg_account(pool: asyncpg.Pool, owner_id: int, phone: str,
                         session_str: str, tg_user_id: int,
                         first_name: str, username: str,
                         device_model: str | None = None,
                         system_version: str | None = None,
                         app_version: str | None = None) -> int:
    row = await pool.fetchrow(
        """INSERT INTO tg_accounts(owner_id, phone, session_str, tg_user_id,
               first_name, username, device_model, system_version, app_version)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
           ON CONFLICT (owner_id, phone) DO UPDATE
           SET session_str=$3, tg_user_id=$4, first_name=$5, username=$6,
               device_model=COALESCE($7, tg_accounts.device_model),
               system_version=COALESCE($8, tg_accounts.system_version),
               app_version=COALESCE($9, tg_accounts.app_version),
               is_active=true, last_used=now()
           RETURNING id""",
        owner_id, phone, session_str, tg_user_id, first_name, username,
        device_model, system_version, app_version,
    )
    return row["id"]


async def remove_tg_account(pool: asyncpg.Pool, acc_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, owner_id,
    )
    return result != "DELETE 0"


async def update_tg_account_used(pool: asyncpg.Pool, acc_id: int) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET last_used=now() WHERE id=$1", acc_id,
    )


async def update_tg_account_status(
    pool: asyncpg.Pool,
    acc_id: int,
    owner_id: int,
    is_active: bool,
) -> bool:
    """Обновляет статус активности аккаунта. Возвращает True если запись найдена и обновлена."""
    result = await pool.execute(
        "UPDATE tg_accounts SET is_active=$3 WHERE id=$1 AND owner_id=$2",
        acc_id, owner_id, is_active,
    )
    return result != "UPDATE 0"


async def get_active_account_for_owner(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    """Возвращает первый активный аккаунт пользователя (используется ranking_checker'ом).

    Всегда фильтруется по owner_id — пользователь видит только свои аккаунты.
    """
    row = await pool.fetchrow(
        "SELECT a.*, p.proxy_url FROM tg_accounts a "
        "LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE "
        "WHERE a.owner_id=$1 AND a.is_active=TRUE "
        "ORDER BY a.last_used DESC NULLS LAST, a.added_at DESC LIMIT 1",
        owner_id,
    )
    return dict(row) if row else None


# ── Search rankings ─────────────────────────────────────────────────────────

async def get_tracked_keywords(pool: asyncpg.Pool, bot_id: int) -> list:
    return await pool.fetch(
        "SELECT id, keyword, is_active, created_at FROM tracked_keywords "
        "WHERE bot_id=$1 ORDER BY created_at",
        bot_id,
    )


async def add_tracked_keyword(pool: asyncpg.Pool, bot_id: int,
                               owner_id: int, keyword: str) -> bool:
    try:
        await pool.execute(
            "INSERT INTO tracked_keywords(bot_id, owner_id, keyword) VALUES($1,$2,$3)",
            bot_id, owner_id, keyword,
        )
        return True
    except Exception:
        return False


async def remove_tracked_keyword(pool: asyncpg.Pool, keyword_id: int,
                                  owner_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM tracked_keywords WHERE id=$1 AND owner_id=$2",
        keyword_id, owner_id,
    )
    return result != "DELETE 0"


async def get_keyword_rankings(pool: asyncpg.Pool, keyword_id: int,
                                limit: int = 10) -> list:
    return await pool.fetch(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT $2",
        keyword_id, limit,
    )


async def get_latest_ranking(pool: asyncpg.Pool, keyword_id: int):
    return await pool.fetchrow(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT 1",
        keyword_id,
    )


async def save_ranking(pool: asyncpg.Pool, keyword_id: int,
                        bot_id: int, position) -> None:
    await pool.execute(
        "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
        keyword_id, bot_id, position,
    )


async def get_ranking_history(pool: asyncpg.Pool, keyword_id: int,
                               limit: int = 7) -> list:
    """Return last N ranking records for a keyword: [(position, checked_at)]."""
    return await pool.fetch(
        "SELECT position, checked_at FROM search_rankings "
        "WHERE keyword_id=$1 ORDER BY checked_at DESC LIMIT $2",
        keyword_id, limit,
    )


async def get_all_keywords_with_latest_ranking(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Возвращает все ключевые слова пользователя с последней позицией и username бота.

    Формат: [{"keyword_id", "keyword", "bot_id", "bot_username", "position", "checked_at"}]
    """
    rows = await pool.fetch(
        """SELECT
               tk.id              AS keyword_id,
               tk.keyword         AS keyword,
               tk.bot_id          AS bot_id,
               mb.username        AS bot_username,
               sr.position        AS position,
               sr.checked_at      AS checked_at
           FROM tracked_keywords tk
           JOIN managed_bots mb ON mb.bot_id = tk.bot_id
           LEFT JOIN LATERAL (
               SELECT position, checked_at
               FROM search_rankings
               WHERE keyword_id = tk.id
               ORDER BY checked_at DESC
               LIMIT 1
           ) sr ON TRUE
           WHERE tk.owner_id = $1
           ORDER BY mb.username, tk.keyword""",
        owner_id,
    )
    return [
        {
            "keyword_id": r["keyword_id"],
            "keyword": r["keyword"],
            "bot_id": r["bot_id"],
            "bot_username": r["bot_username"],
            "position": r["position"],
            "checked_at": r["checked_at"],
        }
        for r in rows
    ]


async def get_bot_owner(pool: asyncpg.Pool, bot_id: int) -> int | None:
    """Возвращает added_by (owner_id) для бота или None если бот не найден."""
    return await pool.fetchval(
        "SELECT added_by FROM managed_bots WHERE bot_id=$1 AND is_active=TRUE",
        bot_id,
    )


async def toggle_keyword_active(pool: asyncpg.Pool, keyword_id: int, owner_id: int) -> bool | None:
    """Переключает is_active ключевого слова. Возвращает новое значение или None если не найдено."""
    row = await pool.fetchrow(
        """UPDATE tracked_keywords
           SET is_active = NOT is_active
           WHERE id = $1 AND owner_id = $2
           RETURNING is_active""",
        keyword_id, owner_id,
    )
    return row["is_active"] if row else None


async def toggle_keyword_notify(pool: asyncpg.Pool, bot_id: int, owner_id: int) -> bool | None:
    """Переключает notify_enabled для всех ключевых слов бота.

    Если хотя бы одно включено — выключает все; иначе — включает все.
    Возвращает новое значение или None если ключевых слов не найдено.
    """
    current = await pool.fetchval(
        "SELECT bool_or(notify_enabled) FROM tracked_keywords WHERE bot_id=$1 AND owner_id=$2",
        bot_id, owner_id,
    )
    if current is None:
        return None
    new_value = not current
    await pool.execute(
        "UPDATE tracked_keywords SET notify_enabled=$3 WHERE bot_id=$1 AND owner_id=$2",
        bot_id, owner_id, new_value,
    )
    return new_value


async def get_keyword_notify_enabled(pool: asyncpg.Pool, bot_id: int, owner_id: int) -> bool:
    """Возвращает True если хотя бы одно ключевое слово бота имеет notify_enabled=TRUE."""
    val = await pool.fetchval(
        "SELECT bool_or(notify_enabled) FROM tracked_keywords WHERE bot_id=$1 AND owner_id=$2",
        bot_id, owner_id,
    )
    return bool(val)


async def upsert_managed_channels(
    pool: asyncpg.Pool, owner_id: int, acc_id: int, channels: list[dict]
) -> None:
    """Сохраняет/обновляет список каналов аккаунта в managed_channels."""
    if not channels:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM managed_channels WHERE owner_id=$1 AND acc_id=$2",
                owner_id, acc_id,
            )
            await conn.executemany(
                """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username, access_hash, type)
                   VALUES($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT (owner_id, channel_id) DO UPDATE
                   SET title=EXCLUDED.title, username=EXCLUDED.username,
                       acc_id=EXCLUDED.acc_id, access_hash=EXCLUDED.access_hash,
                       type=EXCLUDED.type""",
                [
                    (owner_id, acc_id, ch["id"], ch.get("title", ""), ch.get("username", ""), ch.get("access_hash", 0), ch.get("type", "channel"))
                    for ch in channels
                ],
            )


async def get_managed_channels(
    pool: asyncpg.Pool, owner_id: int, acc_id: int | None = None
) -> list[asyncpg.Record]:
    """Возвращает каналы из кэша. Если acc_id задан — только для этого аккаунта."""
    if acc_id is not None:
        return await pool.fetch(
            "SELECT * FROM managed_channels WHERE owner_id=$1 AND acc_id=$2 ORDER BY title",
            owner_id, acc_id,
        )
    return await pool.fetch(
        "SELECT * FROM managed_channels WHERE owner_id=$1 ORDER BY acc_id, title",
        owner_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PLATFORM REFERRAL SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

import random
import string as _string


def _gen_code() -> str:
    chars = _string.ascii_uppercase + _string.digits
    return "inv_" + "".join(random.choices(chars, k=6))


async def get_or_create_referral_code(pool: asyncpg.Pool, user_id: int) -> str:
    """Return existing referral code or create a new unique one."""
    existing = await pool.fetchval(
        "SELECT code FROM platform_referral_codes WHERE user_id=$1", user_id
    )
    if existing:
        return existing
    for _ in range(10):
        code = _gen_code()
        try:
            await pool.execute(
                "INSERT INTO platform_referral_codes(user_id, code) VALUES($1,$2)",
                user_id, code,
            )
            return code
        except Exception:
            continue
    raise RuntimeError("Failed to generate unique referral code")


async def get_user_by_referral_code(pool: asyncpg.Pool, code: str) -> int | None:
    """Return referrer user_id for a given code, or None."""
    return await pool.fetchval(
        "SELECT user_id FROM platform_referral_codes WHERE code=$1", code
    )


async def record_platform_referral(
    pool: asyncpg.Pool, referrer_id: int, referred_id: int
) -> bool:
    """Record a new platform referral. Returns False if already exists or invalid."""
    if referrer_id == referred_id:
        return False
    # Anti-fraud: max 100 referrals per month
    monthly = await pool.fetchval(
        """SELECT COUNT(*) FROM platform_referrals
           WHERE referrer_id=$1 AND created_at >= now() - INTERVAL '30 days'""",
        referrer_id,
    ) or 0
    if monthly >= 100:
        return False
    try:
        await pool.execute(
            "INSERT INTO platform_referrals(referrer_id, referred_id) VALUES($1,$2)",
            referrer_id, referred_id,
        )
        await pool.execute(
            "UPDATE platform_referral_codes SET total_clicks=total_clicks+1 WHERE user_id=$1",
            referrer_id,
        )
        return True
    except Exception:
        return False


async def mark_referral_activated(pool: asyncpg.Pool, referred_id: int) -> int | None:
    """Mark referral as activated (user created first bot). Returns referrer_id or None."""
    row = await pool.fetchrow(
        """UPDATE platform_referrals
           SET activated_at=now()
           WHERE referred_id=$1 AND activated_at IS NULL
           RETURNING referrer_id""",
        referred_id,
    )
    return row["referrer_id"] if row else None


async def mark_referral_paid(pool: asyncpg.Pool, referred_id: int) -> int | None:
    """Mark referral as paid (referred user confirmed payment). Returns referrer_id or None."""
    row = await pool.fetchrow(
        """UPDATE platform_referrals
           SET paid_at=now()
           WHERE referred_id=$1 AND paid_at IS NULL
           RETURNING referrer_id""",
        referred_id,
    )
    return row["referrer_id"] if row else None


async def get_referral_stats(pool: asyncpg.Pool, user_id: int) -> dict:
    """Return referral dashboard data for a user."""
    code = await get_or_create_referral_code(pool, user_id)
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1", user_id
    ) or 0
    active = await pool.fetchval(
        "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1 AND activated_at IS NOT NULL",
        user_id,
    ) or 0
    paid = await pool.fetchval(
        "SELECT COUNT(*) FROM platform_referrals WHERE referrer_id=$1 AND paid_at IS NOT NULL",
        user_id,
    ) or 0
    rewards = await pool.fetch(
        "SELECT level, plan, days, given_at FROM referral_rewards WHERE user_id=$1 ORDER BY given_at",
        user_id,
    )
    return {
        "code": code,
        "total": total,
        "active": active,
        "paid": paid,
        "rewards": [dict(r) for r in rewards],
    }


async def get_referral_leaderboard_platform(pool: asyncpg.Pool, limit: int = 10) -> list[dict]:
    """Top referrers by number of paying referrals."""
    rows = await pool.fetch(
        """SELECT pr.referrer_id, pu.first_name, pu.username,
                  COUNT(*) FILTER (WHERE pr.paid_at IS NOT NULL) AS paid_count,
                  COUNT(*) AS total_count
           FROM platform_referrals pr
           JOIN platform_users pu ON pu.user_id = pr.referrer_id
           GROUP BY pr.referrer_id, pu.first_name, pu.username
           HAVING COUNT(*) FILTER (WHERE pr.paid_at IS NOT NULL) > 0
           ORDER BY paid_count DESC, total_count DESC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]


# Reward tier definitions
_REWARD_TIERS = [
    ("basic",    "active", 5,  "starter", 14),
    ("silver",   "paid",   3,  "starter", 30),
    ("gold",     "paid",   10, "pro",     30),
    ("platinum", "paid",   25, "enterprise", 30),
]


async def check_and_grant_rewards(
    pool: asyncpg.Pool, referrer_id: int, bot
) -> list[str]:
    """Check thresholds and grant any unclaimed rewards. Returns list of newly granted levels."""
    stats = await get_referral_stats(pool, referrer_id)
    granted = []
    existing_levels = {r["level"] for r in stats["rewards"]}

    for level, metric, threshold, plan, days in _REWARD_TIERS:
        if level in existing_levels:
            continue
        count = stats["active"] if metric == "active" else stats["paid"]
        if count < threshold:
            continue
        try:
            await pool.execute(
                "INSERT INTO referral_rewards(user_id, level, plan, days) VALUES($1,$2,$3,$4)",
                referrer_id, level, plan, days,
            )
        except Exception:
            continue
        # Extend subscription
        await pool.execute(
            """INSERT INTO subscriptions (user_id, plan, expires_at, is_active)
               VALUES ($1, $2, now() + ($3 || ' days')::INTERVAL, true)
               ON CONFLICT (user_id) DO UPDATE SET
                   plan = CASE
                       WHEN ARRAY_POSITION(ARRAY['starter','pro','enterprise'], EXCLUDED.plan) >
                            ARRAY_POSITION(ARRAY['starter','pro','enterprise'], subscriptions.plan)
                       THEN EXCLUDED.plan ELSE subscriptions.plan END,
                   is_active  = true,
                   expires_at = GREATEST(subscriptions.expires_at, now())
                              + ($3 || ' days')::INTERVAL""",
            referrer_id, plan, str(days),
        )
        granted.append(level)
        # Notify referrer
        level_names = {
            "basic": "🥉 Базовый", "silver": "🥈 Серебро",
            "gold": "🥇 Золото", "platinum": "💎 Платина",
        }
        plan_names = {"starter": "Starter", "pro": "Pro", "enterprise": "Enterprise"}
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Реферальная награда получена!</b>\n\n"
                f"Уровень: {level_names.get(level, level)}\n"
                f"Награда: <b>{days} дней {plan_names.get(plan, plan)} бесплатно!</b>\n\n"
                f"Продолжайте приглашать — впереди ещё уровни!\n"
                f"/referral — ваш прогресс",
                parse_mode="HTML",
            )
        except Exception:
            log_exc_swallow(log, "Сбой уведомления о реферальной награде", referrer_id=referrer_id, level=level)

    return granted


async def give_welcome_bonus(pool: asyncpg.Pool, referred_id: int, bot) -> bool:
    """Give 7 days Starter to a new user who joined via referral link. One-time only."""
    updated = await pool.fetchval(
        """UPDATE platform_referrals
           SET welcome_bonus_given=true
           WHERE referred_id=$1 AND welcome_bonus_given=false
           RETURNING id""",
        referred_id,
    )
    if not updated:
        return False
    await pool.execute(
        """INSERT INTO subscriptions (user_id, plan, expires_at, is_active)
           VALUES ($1, 'starter', now() + INTERVAL '7 days', true)
           ON CONFLICT (user_id) DO UPDATE SET
               plan       = CASE WHEN subscriptions.is_active THEN subscriptions.plan ELSE 'starter' END,
               is_active  = true,
               expires_at = GREATEST(subscriptions.expires_at, now()) + INTERVAL '7 days'""",
        referred_id,
    )
    try:
        await bot.send_message(
            referred_id,
            "🎁 <b>Подарок от реферальной программы!</b>\n\n"
            "Вы пришли по реферальной ссылке и получаете <b>7 дней Starter бесплатно!</b>\n\n"
            "💡 Поделитесь своей ссылкой — и вы тоже можете получить бесплатный тариф:\n"
            "/referral",
            parse_mode="HTML",
        )
    except Exception:
        log_exc_swallow(log, "Сбой уведомления о реферальном подарке", referred_id=referred_id)
    return True


async def deactivate_account(pool: asyncpg.Pool, account_id: int, reason: str = "") -> None:
    """Mark account as inactive (banned / PeerFlood). Called by operation handlers."""
    await pool.execute(
        "UPDATE tg_accounts SET is_active=false WHERE id=$1",
        account_id,
    )
    if reason:
        log.warning("Account %s deactivated: %s", account_id, reason)


# ── Trust Engine ───────────────────────────────────────────────────────────

async def get_trusted_accounts(
    pool: asyncpg.Pool,
    owner_id: int,
) -> list[asyncpg.Record]:
    """Return active accounts not in cooldown, ordered by trust_score DESC."""
    return await pool.fetch(
        """SELECT a.*, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.is_active=true
             AND (a.cooldown_until IS NULL OR a.cooldown_until < NOW())
           ORDER BY a.trust_score DESC NULLS LAST, a.last_used ASC NULLS FIRST""",
        owner_id,
    )


async def record_flood_event(
    pool: asyncpg.Pool,
    account_id: int,
    operation: str = "",
    flood_seconds: int = 0,
) -> None:
    """Record a flood event: increment counter, set cooldown, log it."""
    cooldown_hours = 2
    if flood_seconds > 3600:
        cooldown_hours = 6
    await pool.execute(
        """UPDATE tg_accounts
           SET flood_count_7d = flood_count_7d + 1,
               last_flood_at  = NOW(),
               cooldown_until = NOW() + ($1 * INTERVAL '1 hour')
           WHERE id = $2""",
        cooldown_hours, account_id,
    )
    await pool.execute(
        """INSERT INTO account_flood_log(account_id, operation, flood_seconds)
           VALUES($1, $2, $3)""",
        account_id, operation, flood_seconds,
    )


async def get_notification_settings(pool: asyncpg.Pool, user_id: int) -> dict:
    """Return notification preferences for a user. Defaults to all True if no record."""
    row = await pool.fetchrow(
        "SELECT new_user, flood_warning, position_change, op_complete, restriction "
        "FROM notification_settings WHERE user_id=$1",
        user_id,
    )
    if row:
        result = dict(row)
        result.setdefault("deploy", True)
        return result
    return {
        "new_user": True,
        "flood_warning": True,
        "position_change": True,
        "op_complete": True,
        "restriction": True,
        "deploy": True,
    }


async def notify_if_enabled(
    pool: asyncpg.Pool,
    bot,
    user_id: int,
    pref: str,
    text: str,
    reply_markup=None,
) -> None:
    """Send a notification to user only if the given preference flag is True."""
    try:
        settings = await get_notification_settings(pool, user_id)
        if not settings.get(pref, True):
            return
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        log_exc_swallow(log, "Сбой notify_if_enabled", user_id=user_id, pref=pref)


# ── Global Presence Factory ────────────────────────────────────────────────

async def create_global_presence_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    asset_type: str,
    name_pattern: str,
    username_pattern: str | None,
    geo_selection: dict,
    account_selection: dict,
    template_id: int | None = None,
) -> int:
    """Create a plan and return its id."""
    import json
    return await pool.fetchval(
        """INSERT INTO global_presence_plans
               (owner_id, asset_type, name_pattern, username_pattern,
                geo_selection, account_selection, template_id, status)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,'queued')
           RETURNING id""",
        owner_id, asset_type, name_pattern, username_pattern,
        json.dumps(geo_selection), json.dumps(account_selection), template_id,
    )


async def create_global_presence_targets(
    pool: asyncpg.Pool, plan_id: int, targets: list[dict]
) -> int:
    """Bulk-insert targets for a plan. Returns count inserted."""
    if not targets:
        return 0
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO global_presence_targets
                   (plan_id, country, country_code, region, city, city_slug,
                    language, timezone, asset_type, planned_name, planned_username,
                    selected_account_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            [
                (
                    plan_id,
                    t.get("country"), t.get("country_code"), t.get("region"),
                    t.get("city"), t.get("city_slug"), t.get("language"), t.get("timezone"),
                    t.get("asset_type", "channel"),
                    t.get("planned_name"), t.get("planned_username"),
                    t.get("selected_account_id"),
                )
                for t in targets
            ],
        )
    return len(targets)


async def get_global_presence_plan(
    pool: asyncpg.Pool, plan_id: int, owner_id: int
) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
        plan_id, owner_id,
    )


async def get_global_presence_plans(
    pool: asyncpg.Pool, owner_id: int, limit: int = 10, offset: int = 0
) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM global_presence_plans WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
        owner_id, limit, offset,
    )


async def get_global_presence_stats(pool: asyncpg.Pool, plan_id: int) -> dict:
    row = await pool.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE status='pending')  AS pending,
               COUNT(*) FILTER (WHERE status='done')     AS done,
               COUNT(*) FILTER (WHERE status='failed')   AS failed,
               COUNT(*) FILTER (WHERE status='running')  AS running,
               COUNT(*) AS total
           FROM global_presence_targets WHERE plan_id=$1""",
        plan_id,
    )
    return dict(row) if row else {"pending": 0, "done": 0, "failed": 0, "running": 0, "total": 0}


async def reset_failed_targets(pool: asyncpg.Pool, plan_id: int) -> int:
    """Reset failed+retryable targets to pending for retry. Returns count reset."""
    result = await pool.execute(
        "UPDATE global_presence_targets SET status='pending', error_message=NULL "
        "WHERE plan_id=$1 AND status='failed' AND retryable=TRUE",
        plan_id,
    )
    return int(result.split()[-1]) if result else 0


async def link_plan_to_operation(pool: asyncpg.Pool, plan_id: int, op_id: int) -> None:
    await pool.execute(
        "UPDATE global_presence_plans SET op_id=$1, status='queued', updated_at=now() WHERE id=$2",
        op_id, plan_id,
    )


async def cancel_global_presence_plan(
    pool: asyncpg.Pool, plan_id: int, owner_id: int
) -> bool:
    """Cancel a plan and its linked operation. Returns True if the plan was found and owned."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            plan = await conn.fetchrow(
                "SELECT op_id FROM global_presence_plans WHERE id=$1 AND owner_id=$2",
                plan_id, owner_id,
            )
            if not plan:
                return False
            await conn.execute(
                "UPDATE global_presence_plans SET status='cancelled', updated_at=now() WHERE id=$1",
                plan_id,
            )
            await conn.execute(
                "UPDATE global_presence_targets SET status='cancelled' WHERE plan_id=$1 AND status='pending'",
                plan_id,
            )
            if plan["op_id"]:
                await conn.execute(
                    "UPDATE operation_queue SET status='cancelled', finished_at=now() "
                    "WHERE id=$1 AND status NOT IN ('done','failed','cancelled')",
                    plan["op_id"],
                )
    return True


async def sync_plan_status_from_op(pool: asyncpg.Pool, plan_id: int) -> str | None:
    """If operation is done/failed but plan still running, fix plan status. Returns new status or None."""
    row = await pool.fetchrow(
        """SELECT p.status AS plan_status, p.op_id, o.status AS op_status
           FROM global_presence_plans p
           LEFT JOIN operation_queue o ON o.id=p.op_id
           WHERE p.id=$1""",
        plan_id,
    )
    if not row:
        return None
    if row["plan_status"] not in ("running", "queued"):
        return None
    op_status = row["op_status"]
    if op_status in ("done", "failed", "cancelled"):
        new_status = op_status
        await pool.execute(
            "UPDATE global_presence_plans SET status=$1, updated_at=now() WHERE id=$2",
            new_status, plan_id,
        )
        return new_status
    return None


# ── Operation Reports and Statistics ──────────────────────────────────────

async def get_operation_stats(pool: asyncpg.Pool, owner_id: int, op_id: int) -> dict:
    """Получить полную статистику выполненной операции."""
    op = await pool.fetchrow(
        "SELECT id, op_type, status, total_items, done_items, params, created_at, updated_at "
        "FROM operation_queue WHERE id=$1 AND owner_id=$2",
        op_id, owner_id,
    )
    if not op:
        return {}

    logs = await pool.fetch(
        "SELECT step_num, target, status, message FROM operation_log "
        "WHERE op_id=$1 ORDER BY step_num",
        op_id,
    )

    errors = [l for l in logs if l["status"] != "ok"]
    success_count = len([l for l in logs if l["status"] == "ok"])

    return {
        "id": op["id"],
        "type": op["op_type"],
        "status": op["status"],
        "total": op["total_items"],
        "done": op["done_items"],
        "success": success_count,
        "errors": len(errors),
        "created_at": op["created_at"],
        "updated_at": op["updated_at"],
        "duration_seconds": (op["updated_at"] - op["created_at"]).total_seconds() if op["updated_at"] else 0,
        "error_details": errors[:10],  # top 10 errors
    }


async def get_user_operation_history(
    pool: asyncpg.Pool, owner_id: int, limit: int = 20
) -> list[dict]:
    """Получить историю операций пользователя."""
    rows = await pool.fetch(
        "SELECT id, op_type, status, total_items, done_items, created_at "
        "FROM operation_queue WHERE owner_id=$1 "
        "ORDER BY created_at DESC LIMIT $2",
        owner_id, limit,
    )
    return [dict(r) for r in rows]


async def count_operation_errors(pool: asyncpg.Pool, op_id: int) -> int:
    """Сколько ошибок в логе операции."""
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM operation_log WHERE op_id=$1 AND status != 'ok'",
        op_id,
    )
    return count or 0


# ── Platform Users Management (v39) ──────────────────────────────────────────

async def register_or_update_user(
    pool: asyncpg.Pool, user_id: int, username: str = None, first_name: str = None
) -> None:
    """Регистрировать нового или обновить существующего пользователя."""
    try:
        await pool.execute(
            """INSERT INTO platform_users (user_id, username, first_name, last_seen)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (user_id) DO UPDATE
               SET username = COALESCE($2, platform_users.username),
                   first_name = COALESCE($3, platform_users.first_name),
                   last_seen = now()""",
            user_id, username, first_name,
        )
    except asyncpg.UndefinedColumnError:
        # Совместимость: старая схема v14 использует last_active
        await pool.execute(
            """INSERT INTO platform_users (user_id, username, first_name, last_active)
               VALUES ($1, $2, $3, now())
               ON CONFLICT (user_id) DO UPDATE
               SET username = COALESCE($2, platform_users.username),
                   first_name = COALESCE($3, platform_users.first_name),
                   last_active = now()""",
            user_id, username, first_name,
        )


async def get_all_platform_users(
    pool: asyncpg.Pool, limit: int = 50, offset: int = 0, plan: str = None, is_banned: bool = None
) -> list[dict]:
    """Получить список всех пользователей с фильтрацией."""
    query = (
        "SELECT user_id, username, first_name, "
        "COALESCE(current_plan, 'free') as current_plan, "
        "plan_expires_at, "
        "COALESCE(is_banned, false) as is_banned, "
        "COALESCE(registered_at, first_seen, created_at) as registered_at "
        "FROM platform_users WHERE 1=1"
    )
    params = []

    if plan:
        query += " AND COALESCE(current_plan,'free')=$" + str(len(params) + 1)
        params.append(plan)

    if is_banned is not None:
        query += " AND COALESCE(is_banned,false)=$" + str(len(params) + 1)
        params.append(is_banned)

    query += " ORDER BY COALESCE(registered_at, first_seen) DESC NULLS LAST LIMIT $" + str(len(params) + 1) + " OFFSET $" + str(len(params) + 2)
    params.extend([limit, offset])

    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


async def grant_plan_to_user(
    pool: asyncpg.Pool, user_id: int, admin_id: int, plan: str, months: int
) -> None:
    """Выдать план пользователю (админ-действие). Пишет в обе таблицы."""
    from datetime import datetime, timedelta
    expires = datetime.utcnow() + timedelta(days=30 * months)

    # Обновить platform_users
    await pool.execute(
        """UPDATE platform_users SET current_plan=$1, plan_expires_at=$2 WHERE user_id=$3""",
        plan, expires, user_id,
    )

    # Обновить subscriptions (именно здесь get_plan() проверяет доступ)
    if plan == "free":
        await pool.execute(
            "UPDATE subscriptions SET is_active=false WHERE user_id=$1", user_id
        )
    else:
        await pool.execute(
            """INSERT INTO subscriptions(user_id, plan, expires_at, is_active)
               VALUES($1,$2,$3,true)
               ON CONFLICT(user_id) DO UPDATE SET plan=$2, expires_at=$3, is_active=true""",
            user_id, plan, expires,
        )

    # Логировать действие
    try:
        await pool.execute(
            """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
               VALUES ($1, 'grant_plan', $2, $3)""",
            admin_id, user_id,
            json.dumps({"plan": plan, "months": months, "expires_at": expires.isoformat()}),
        )
    except Exception:
        log_exc_swallow(log, "Сбой записи admin_audit_log", admin_id=admin_id, user_id=user_id)


async def revoke_plan_from_user(pool: asyncpg.Pool, user_id: int, admin_id: int) -> None:
    """Забрать подписку у пользователя (вернуть на free)."""
    await pool.execute(
        "UPDATE platform_users SET current_plan='free', plan_expires_at=NULL WHERE user_id=$1",
        user_id,
    )
    # Отключить в subscriptions (именно здесь get_plan() проверяет)
    await pool.execute(
        "UPDATE subscriptions SET is_active=false WHERE user_id=$1", user_id
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'revoke_plan', $2, '{}')""",
        admin_id, user_id,
    )


async def revoke_strike_access(pool: asyncpg.Pool, user_id: int, admin_id: int) -> None:
    """Забрать Strike доступ у пользователя."""
    await pool.execute(
        "DELETE FROM strike_access WHERE user_id=$1", user_id
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'revoke_strike', $2, '{}')""",
        admin_id, user_id,
    )


async def ban_user(pool: asyncpg.Pool, user_id: int, admin_id: int, reason: str = None) -> None:
    """Забанить пользователя."""
    await pool.execute(
        """UPDATE platform_users
           SET is_banned=true, ban_reason=$1, banned_at=now()
           WHERE user_id=$2""",
        reason, user_id,
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'ban_user', $2, $3)""",
        admin_id, user_id,
        '{"reason":"' + (reason or "") + '"}',
    )


async def unban_user(pool: asyncpg.Pool, user_id: int, admin_id: int) -> None:
    """Разбанить пользователя."""
    await pool.execute(
        """UPDATE platform_users
           SET is_banned=false, ban_reason=NULL, banned_at=NULL
           WHERE user_id=$1""",
        user_id,
    )

    # Логировать действие
    await pool.execute(
        """INSERT INTO admin_audit_log (admin_id, action, target_user_id, details)
           VALUES ($1, 'unban_user', $2, '{}')""",
        admin_id, user_id,
    )


async def get_user_info(pool: asyncpg.Pool, user_id: int) -> dict:
    """Получить полную информацию о пользователе."""
    row = await pool.fetchrow(
        "SELECT * FROM platform_users WHERE user_id=$1",
        user_id,
    )
    return dict(row) if row else None


async def log_security_violation(
    pool: asyncpg.Pool, user_id: int, attempt_type: str, details: dict = None, ip_address: str = None
) -> None:
    """Логировать попытку несанкционированного доступа или подозрительную активность."""
    await pool.execute(
        """INSERT INTO security_violations (user_id, attempt_type, details, ip_address)
           VALUES ($1, $2, $3, $4)""",
        user_id, attempt_type, str(details or {}), ip_address,
    )


async def count_platform_users(pool: asyncpg.Pool, plan: str = None, is_banned: bool = None) -> int:
    """Подсчитать количество пользователей с фильтрацией."""
    query = "SELECT COUNT(*) FROM platform_users WHERE 1=1"
    params = []

    if plan:
        query += " AND current_plan=$" + str(len(params) + 1)
        params.append(plan)

    if is_banned is not None:
        query += " AND is_banned=$" + str(len(params) + 1)
        params.append(is_banned)

    count = await pool.fetchval(query, *params)
    return count or 0


async def get_admin_audit_log(
    pool: asyncpg.Pool, admin_id: int = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Получить лог админ-действий."""
    if admin_id:
        rows = await pool.fetch(
            """SELECT id, admin_id, action, target_user_id, details, created_at
               FROM admin_audit_log
               WHERE admin_id=$1
               ORDER BY created_at DESC LIMIT $2 OFFSET $3""",
            admin_id, limit, offset,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, admin_id, action, target_user_id, details, created_at
               FROM admin_audit_log
               ORDER BY created_at DESC LIMIT $1 OFFSET $2""",
            limit, offset,
        )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# BOT ADMIN SESSIONS
# ══════════════════════════════════════════════════════════════════

async def upsert_bot_admin_session(pool: asyncpg.Pool, bot_id: int, owner_id: int, token: str) -> None:
    await pool.execute(
        "INSERT INTO bot_admin_sessions(bot_id,owner_id,token) VALUES($1,$2,$3) "
        "ON CONFLICT(bot_id) DO UPDATE SET token=$3, owner_id=$2",
        bot_id, owner_id, token,
    )


async def get_bot_admin_session_by_token(pool: asyncpg.Pool, token: str):
    return await pool.fetchrow(
        "SELECT bot_id, owner_id FROM bot_admin_sessions WHERE token=$1", token
    )


async def get_bot_admin_token(pool: asyncpg.Pool, bot_id: int) -> str | None:
    row = await pool.fetchrow("SELECT token FROM bot_admin_sessions WHERE bot_id=$1", bot_id)
    return row["token"] if row else None


# ══════════════════════════════════════════════════════════════════
# PRESENCE PACKS
# ══════════════════════════════════════════════════════════════════

async def create_presence_pack(
    pool: asyncpg.Pool, owner_id: int, name: str,
    description: str | None = None, target_url: str | None = None,
    target_label: str | None = None, bot_id: int | None = None,
    bot_username: str | None = None,
) -> int:
    return await pool.fetchval(
        "INSERT INTO presence_packs(owner_id,name,description,target_url,target_label,bot_id,bot_username) "
        "VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id",
        owner_id, name, description, target_url, target_label, bot_id, bot_username,
    )


async def get_presence_pack(pool: asyncpg.Pool, pack_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, owner_id
    )


async def get_presence_packs(pool: asyncpg.Pool, owner_id: int, limit: int = 15):
    return await pool.fetch(
        "SELECT * FROM presence_packs WHERE owner_id=$1 ORDER BY created_at DESC LIMIT $2",
        owner_id, limit,
    )


async def update_presence_pack_channels(
    pool: asyncpg.Pool, pack_id: int, owner_id: int,
    channel_ids: list[int], group_ids: list[int],
) -> None:
    import json
    await pool.execute(
        "UPDATE presence_packs SET channel_ids=$3, group_ids=$4 WHERE id=$1 AND owner_id=$2",
        pack_id, owner_id, json.dumps(channel_ids), json.dumps(group_ids),
    )


async def mark_presence_pack_seeded(pool: asyncpg.Pool, pack_id: int, owner_id: int) -> None:
    await pool.execute(
        "UPDATE presence_packs SET seed_posted=TRUE WHERE id=$1 AND owner_id=$2",
        pack_id, owner_id,
    )


async def mark_presence_pack_promoted(pool: asyncpg.Pool, pack_id: int, owner_id: int) -> None:
    await pool.execute(
        "UPDATE presence_packs SET bot_promoted=TRUE WHERE id=$1 AND owner_id=$2",
        pack_id, owner_id,
    )


async def delete_presence_pack(pool: asyncpg.Pool, pack_id: int, owner_id: int) -> None:
    await pool.execute(
        "DELETE FROM presence_packs WHERE id=$1 AND owner_id=$2", pack_id, owner_id
    )


async def enqueue_op_with_approval(
    pool: asyncpg.Pool,
    owner_id: int,
    op_type: str,
    params: dict,
    total_items: int,
    threshold: int = 20,
) -> int:
    """Enqueue operation. If total_items > threshold, set requires_approval=TRUE."""
    import json as _json
    needs_approval = total_items > threshold
    status = "waiting_approval" if needs_approval else "pending"
    row = await pool.fetchrow(
        """INSERT INTO operation_queue
           (owner_id, op_type, params, total_items, status, requires_approval, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,now()) RETURNING id""",
        owner_id, op_type,
        _json.dumps(params) if isinstance(params, dict) else params,
        total_items, status, needs_approval,
    )
    return row["id"]


async def get_pending_approvals(pool: asyncpg.Pool, owner_id: int) -> list:
    return await pool.fetch(
        """SELECT id, op_type, total_items, created_at
           FROM operation_queue
           WHERE owner_id=$1 AND status='waiting_approval'
           ORDER BY created_at DESC LIMIT 10""",
        owner_id,
    )


# ── Workspaces ────────────────────────────────────────────────────────────────

async def create_workspace(pool: asyncpg.Pool, owner_id: int, name: str, description: str = "") -> int:
    import secrets as _sec
    row = await pool.fetchrow(
        "INSERT INTO workspaces (owner_id, name, description) VALUES ($1,$2,$3) RETURNING id",
        owner_id, name[:64], description[:256],
    )
    ws_id = row["id"]
    await pool.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role, invited_by) VALUES ($1,$2,'owner',$2)",
        ws_id, owner_id,
    )
    return ws_id


async def get_user_workspaces(pool: asyncpg.Pool, user_id: int) -> list:
    return await pool.fetch(
        """SELECT w.id, w.name, w.description, wm.role, w.owner_id,
                  (SELECT COUNT(*) FROM workspace_members WHERE workspace_id=w.id) AS member_count
           FROM workspaces w
           JOIN workspace_members wm ON wm.workspace_id=w.id AND wm.user_id=$1
           WHERE w.is_active=TRUE
           ORDER BY w.created_at""",
        user_id,
    )


async def get_workspace(pool: asyncpg.Pool, ws_id: int) -> dict | None:
    row = await pool.fetchrow("SELECT * FROM workspaces WHERE id=$1 AND is_active=TRUE", ws_id)
    return dict(row) if row else None


async def get_workspace_members(pool: asyncpg.Pool, ws_id: int) -> list:
    return await pool.fetch(
        """SELECT wm.user_id, wm.role, wm.joined_at, pu.username, pu.first_name
           FROM workspace_members wm
           LEFT JOIN platform_users pu ON pu.user_id=wm.user_id
           WHERE wm.workspace_id=$1 ORDER BY wm.joined_at""",
        ws_id,
    )


async def create_workspace_invite(pool: asyncpg.Pool, ws_id: int, created_by: int) -> str:
    import secrets as _sec
    code = _sec.token_urlsafe(12)
    await pool.execute(
        "INSERT INTO workspace_invites (workspace_id, invite_code, created_by, uses_left) VALUES ($1,$2,$3,5)",
        ws_id, code, created_by,
    )
    return code


async def use_workspace_invite(pool: asyncpg.Pool, code: str, user_id: int) -> int | None:
    """Use invite code. Returns workspace_id on success, None if invalid/expired."""
    invite = await pool.fetchrow(
        "SELECT * FROM workspace_invites WHERE invite_code=$1 AND uses_left>0",
        code,
    )
    if not invite:
        return None
    ws_id = invite["workspace_id"]
    existing = await pool.fetchval(
        "SELECT 1 FROM workspace_members WHERE workspace_id=$1 AND user_id=$2", ws_id, user_id
    )
    if not existing:
        await pool.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role, invited_by) VALUES ($1,$2,'member',$3)",
            ws_id, user_id, invite["created_by"],
        )
        await pool.execute(
            "UPDATE workspace_invites SET uses_left=uses_left-1 WHERE invite_code=$1", code
        )
    return ws_id


async def delete_workspace_member(pool: asyncpg.Pool, ws_id: int, user_id: int) -> None:
    await pool.execute(
        "DELETE FROM workspace_members WHERE workspace_id=$1 AND user_id=$2", ws_id, user_id
    )


async def get_platform_setting(pool: asyncpg.Pool, key: str, default: str = "") -> str:
    row = await pool.fetchrow(
        "SELECT value FROM platform_settings WHERE key=$1", key
    )
    return row["value"] if row else default


async def set_platform_setting(pool: asyncpg.Pool, key: str, value: str) -> None:
    await pool.execute(
        """INSERT INTO platform_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()""",
        key, value,
    )


# ── Account Infrastructure (v60) ─────────────────────────────────────────────

async def update_account_tags(pool: asyncpg.Pool, acc_id: int, owner_id: int, tags: list[str]) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET tags=$1 WHERE id=$2 AND owner_id=$3",
        tags, acc_id, owner_id,
    )


async def update_account_pool(pool: asyncpg.Pool, acc_id: int, owner_id: int, pool_name: str | None) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET pool=$1 WHERE id=$2 AND owner_id=$3",
        pool_name, acc_id, owner_id,
    )


async def update_account_labels(pool: asyncpg.Pool, acc_id: int, owner_id: int, labels: list[str]) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET labels=$1 WHERE id=$2 AND owner_id=$3",
        labels, acc_id, owner_id,
    )


async def update_account_warnings(pool: asyncpg.Pool, acc_id: int, owner_id: int, warnings: list[str]) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET warnings=$1 WHERE id=$2 AND owner_id=$3",
        warnings, acc_id, owner_id,
    )


async def update_account_project(pool: asyncpg.Pool, acc_id: int, owner_id: int, project: str | None) -> None:
    await pool.execute(
        "UPDATE tg_accounts SET project=$1 WHERE id=$2 AND owner_id=$3",
        project, acc_id, owner_id,
    )


async def get_accounts_by_pool(pool: asyncpg.Pool, owner_id: int, pool_name: str) -> list:
    return await pool.fetch(
        """SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                  a.trust_score, a.cooldown_until, a.tags, a.pool, a.labels, a.warnings, a.project,
                  a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.pool=$2 AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST""",
        owner_id, pool_name,
    )


async def get_accounts_by_tags(pool: asyncpg.Pool, owner_id: int, tags: list[str]) -> list:
    """Return accounts that have ALL of the specified tags."""
    return await pool.fetch(
        """SELECT a.id, a.phone, a.first_name, a.username, a.session_str, a.is_active,
                  a.trust_score, a.cooldown_until, a.tags, a.pool, a.labels, a.warnings, a.project,
                  a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.owner_id=$1 AND a.tags @> $2::text[] AND a.is_active=TRUE
           ORDER BY a.trust_score DESC NULLS LAST""",
        owner_id, tags,
    )


async def get_distinct_pools(pool: asyncpg.Pool, owner_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT pool FROM tg_accounts WHERE owner_id=$1 AND pool IS NOT NULL AND is_active=TRUE ORDER BY pool",
        owner_id,
    )
    return [r["pool"] for r in rows]


async def get_distinct_tags(pool: asyncpg.Pool, owner_id: int) -> list[str]:
    rows = await pool.fetch(
        "SELECT DISTINCT unnest(tags) AS tag FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE ORDER BY tag",
        owner_id,
    )
    return [r["tag"] for r in rows]


async def get_account_assets(pool: asyncpg.Pool, acc_id: int, owner_id: int) -> dict:
    """Return all assets associated with an account (for disaster recovery)."""
    channels = await pool.fetch(
        "SELECT channel_id, title, username FROM managed_channels WHERE acc_id=$1 AND owner_id=$2 ORDER BY title",
        acc_id, owner_id,
    )
    active_ops = await pool.fetch(
        """SELECT id, op_type, status, total_items, done_items, created_at
           FROM operation_queue
           WHERE owner_id=$1 AND status IN ('pending','running')
             AND params::text LIKE $2
           ORDER BY created_at DESC LIMIT 20""",
        owner_id, f'%"acc_id": {acc_id}%',
    )
    return {"channels": list(channels), "ops": list(active_ops)}


# ── Proxy Intelligence (v60) ──────────────────────────────────────────────────

async def log_proxy_quality(
    pool: asyncpg.Pool, proxy_id: int, latency_ms: int | None, success: bool, error_msg: str | None = None
) -> None:
    await pool.execute(
        "INSERT INTO proxy_quality_log (proxy_id, latency_ms, success, error_msg) VALUES ($1,$2,$3,$4)",
        proxy_id, latency_ms, success, error_msg,
    )


async def get_proxy_quality_stats(pool: asyncpg.Pool, proxy_id: int) -> dict:
    row = await pool.fetchrow(
        """SELECT
               COUNT(*) FILTER (WHERE success) AS successes,
               COUNT(*) FILTER (WHERE NOT success) AS failures,
               COUNT(*) AS total,
               AVG(latency_ms) FILTER (WHERE success AND latency_ms IS NOT NULL) AS avg_latency,
               MAX(checked_at) AS last_checked
           FROM proxy_quality_log
           WHERE proxy_id=$1 AND checked_at > NOW() - INTERVAL '7 days'""",
        proxy_id,
    )
    if not row or row["total"] == 0:
        return {"success_rate": None, "avg_latency": None, "total": 0, "last_checked": None}
    total = row["total"] or 1
    return {
        "success_rate": round((row["successes"] or 0) / total * 100, 1),
        "avg_latency": round(row["avg_latency"]) if row["avg_latency"] else None,
        "total": total,
        "successes": row["successes"] or 0,
        "failures": row["failures"] or 0,
        "last_checked": row["last_checked"],
    }


async def get_all_proxy_quality_stats(pool: asyncpg.Pool, owner_id: int) -> list:
    """Return quality stats for all proxies owned by user."""
    rows = await pool.fetch(
        """SELECT p.id, p.label, p.proxy_url,
               COUNT(q.id) FILTER (WHERE q.success) AS successes,
               COUNT(q.id) FILTER (WHERE NOT q.success) AS failures,
               COUNT(q.id) AS total,
               AVG(q.latency_ms) FILTER (WHERE q.success AND q.latency_ms IS NOT NULL) AS avg_latency
           FROM user_proxies p
           LEFT JOIN proxy_quality_log q
               ON q.proxy_id=p.id AND q.checked_at > NOW() - INTERVAL '7 days'
           WHERE p.owner_id=$1
           GROUP BY p.id, p.label, p.proxy_url
           ORDER BY p.label""",
        owner_id,
    )
    result = []
    for r in rows:
        total = r["total"] or 0
        result.append({
            "id": r["id"],
            "label": r["label"],
            "proxy_url": r["proxy_url"],
            "success_rate": round((r["successes"] or 0) / total * 100, 1) if total > 0 else None,
            "avg_latency": round(r["avg_latency"]) if r["avg_latency"] else None,
            "total": total,
        })
    return result


# ── Infrastructure Pressure Score cache ──────────────────────────────────────

async def save_pressure_cache(pool: asyncpg.Pool, owner_id: int, score: int, breakdown: dict) -> None:
    import json
    await pool.execute(
        """INSERT INTO infra_pressure_cache (owner_id, pressure_score, breakdown, computed_at)
           VALUES ($1, $2, $3, NOW())
           ON CONFLICT (owner_id) DO UPDATE
           SET pressure_score=$2, breakdown=$3, computed_at=NOW()""",
        owner_id, score, json.dumps(breakdown),
    )


async def get_pressure_cache(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    row = await pool.fetchrow(
        "SELECT pressure_score, breakdown, computed_at FROM infra_pressure_cache WHERE owner_id=$1",
        owner_id,
    )
    if not row:
        return None
    import json
    breakdown = row["breakdown"]
    if isinstance(breakdown, str):
        breakdown = json.loads(breakdown)
    return {
        "score": row["pressure_score"],
        "breakdown": breakdown,
        "computed_at": row["computed_at"],
    }
