"""BotMother Nodes Engine — Telegram Forum Workspace management.

Architecture:
  bm_telegram_nodes  → root workspace (forum supergroup + node_type)
  bm_node_threads    → topic per infrastructure entity (proxy/account/worker)

Telegram Forum API used:
  bot.create_forum_topic(chat_id, name, icon_color)  → ForumTopic.message_thread_id
  bot.close_forum_topic(chat_id, message_thread_id)
  bot.reopen_forum_topic(chat_id, message_thread_id)
  bot.send_message(..., message_thread_id=N)

Enable forum mode on a supergroup requires MTProto:
  ToggleForumRequest via Telethon — see enable_forum_mode().
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

NODE_TYPE_LABELS: dict[str, str] = {
    "proxies":  "🌐 Прокси-воркспейс",
    "accounts": "👤 Аккаунт-воркспейс",
    "tasks":    "📋 Задачи-воркспейс",
    "alerts":   "🚨 Алерты-воркспейс",
}

ENTITY_LABELS: dict[str, str] = {
    "proxy":   "🌐 Прокси",
    "account": "👤 Аккаунт",
    "worker":  "⚙️ Воркер",
}

# Telegram topic icon colors (0xRRGGBB, from API docs)
_TOPIC_COLORS: dict[str, int] = {
    "proxy":   0x6FB9F0,   # light blue
    "account": 0xFFD67E,   # yellow
    "worker":  0xCB86DB,   # purple
    "default": 0xA0DC67,   # green
}

# Rate limiting: ≤2.5 forum topic API requests/sec
_FORUM_DELAY = 0.42

# Broadcast chunk settings
_BROADCAST_CHUNK = 25
_BROADCAST_CHUNK_DELAY = 1.2

_ENTITY_TO_NODE: dict[str, str] = {
    "proxy":   "proxies",
    "account": "accounts",
    "worker":  "tasks",
}


# ── Workspace management ──────────────────────────────────────────────────────

async def initialize_workspace(
    pool: asyncpg.Pool,
    bot: Bot,
    owner_id: int,
    tg_chat_id: int,
    node_type: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Register a Telegram forum supergroup as a BotMother Node workspace.

    Returns the node record dict. Raises ValueError on unknown node_type.
    The group must already be a forum supergroup (is_forum=True).
    Use enable_forum_mode() first if needed.
    """
    if node_type not in NODE_TYPE_LABELS:
        raise ValueError(f"Unknown node_type '{node_type}'. Valid: {list(NODE_TYPE_LABELS)}")

    node_name = name or NODE_TYPE_LABELS[node_type]

    # Sanity-check chat access (non-fatal)
    try:
        chat = await bot.get_chat(tg_chat_id)
        if not getattr(chat, "is_forum", False):
            log.warning(
                "nodes_engine: chat %d is not a forum supergroup — "
                "register anyway; enable forum mode manually or via enable_forum_mode()",
                tg_chat_id,
            )
    except Exception as exc:
        log.warning("nodes_engine: can't verify chat %d: %s", tg_chat_id, exc)

    row = await pool.fetchrow(
        """
        INSERT INTO bm_telegram_nodes (owner_id, tg_chat_id, node_type, name, is_active)
        VALUES ($1, $2, $3, $4, TRUE)
        ON CONFLICT (owner_id, tg_chat_id, node_type)
        DO UPDATE SET name = EXCLUDED.name, is_active = TRUE
        RETURNING *
        """,
        owner_id, tg_chat_id, node_type, node_name,
    )
    log.info(
        "nodes_engine: workspace registered node_id=%d owner=%d chat=%d type=%s",
        row["id"], owner_id, tg_chat_id, node_type,
    )
    return dict(row)


async def enable_forum_mode(
    pool: asyncpg.Pool,
    owner_id: int,
    tg_chat_id: int,
) -> bool:
    """Enable forum mode on a supergroup via Telethon MTProto (requires owner account).

    Returns True on success.
    """
    try:
        from services.account_manager import _make_client
        from services.resource_selector import select_account
        from telethon.tl.functions.channels import ToggleForumRequest

        account = await select_account(pool, owner_id)
        if not account:
            log.warning("nodes_engine: no account available for owner=%d", owner_id)
            return False

        client = _make_client(account.get("session_str", ""), account)
        await client.connect()
        try:
            entity = await client.get_entity(tg_chat_id)
            await client(ToggleForumRequest(channel=entity, enabled=True))
            log.info(
                "nodes_engine: forum enabled on chat=%d via account=%d",
                tg_chat_id, account["id"],
            )
            return True
        finally:
            await client.disconnect()
    except Exception as exc:
        log.error("nodes_engine: enable_forum_mode failed chat=%d: %s", tg_chat_id, exc)
        return False


async def deactivate_workspace(
    pool: asyncpg.Pool,
    node_id: int,
    owner_id: int,
) -> bool:
    result = await pool.execute(
        "UPDATE bm_telegram_nodes SET is_active=FALSE WHERE id=$1 AND owner_id=$2",
        node_id, owner_id,
    )
    return result == "UPDATE 1"


# ── Thread provisioning ───────────────────────────────────────────────────────

async def provision_thread_for_entity(
    pool: asyncpg.Pool,
    bot: Bot,
    owner_id: int,
    entity_type: str,
    entity_id: int,
    topic_name: str,
    tg_chat_id: int | None = None,
) -> dict[str, Any] | None:
    """Create a forum topic for an infrastructure entity and persist it.

    Idempotent: returns existing open thread if it already exists.
    Returns thread record or None on error.
    """
    node_type = _ENTITY_TO_NODE.get(entity_type, entity_type)

    if tg_chat_id is not None:
        node = await pool.fetchrow(
            """SELECT * FROM bm_telegram_nodes
               WHERE owner_id=$1 AND tg_chat_id=$2 AND node_type=$3 AND is_active=TRUE""",
            owner_id, tg_chat_id, node_type,
        )
    else:
        node = await pool.fetchrow(
            """SELECT * FROM bm_telegram_nodes
               WHERE owner_id=$1 AND node_type=$2 AND is_active=TRUE
               ORDER BY id LIMIT 1""",
            owner_id, node_type,
        )

    if not node:
        log.warning(
            "nodes_engine: no active workspace for owner=%d entity_type=%s",
            owner_id, entity_type,
        )
        return None

    node_id = node["id"]
    chat_id = node["tg_chat_id"]

    # Return existing open thread (idempotent)
    existing = await pool.fetchrow(
        """SELECT t.*, $4::BIGINT AS tg_chat_id
           FROM bm_node_threads t
           WHERE t.node_id=$1 AND t.entity_type=$2 AND t.entity_id=$3 AND t.status='open'""",
        node_id, entity_type, entity_id, chat_id,
    )
    if existing:
        return dict(existing)

    # Create Telegram forum topic
    color = _TOPIC_COLORS.get(entity_type, _TOPIC_COLORS["default"])
    safe_name = topic_name[:128]
    try:
        topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name=safe_name,
            icon_color=color,
        )
    except Exception as exc:
        log.error(
            "nodes_engine: create_forum_topic failed chat=%d entity=%s/%d: %s",
            chat_id, entity_type, entity_id, exc,
        )
        return None

    thread_id = topic.message_thread_id

    row = await pool.fetchrow(
        """
        INSERT INTO bm_node_threads
            (node_id, tg_thread_id, entity_type, entity_id, topic_name, status)
        VALUES ($1, $2, $3, $4, $5, 'open')
        ON CONFLICT (node_id, entity_type, entity_id)
        DO UPDATE SET tg_thread_id = EXCLUDED.tg_thread_id,
                      status       = 'open',
                      topic_name   = EXCLUDED.topic_name
        RETURNING *
        """,
        node_id, thread_id, entity_type, entity_id, safe_name,
    )
    log.info(
        "nodes_engine: thread provisioned entity=%s/%d thread_id=%d node=%d",
        entity_type, entity_id, thread_id, node_id,
    )
    return {**dict(row), "tg_chat_id": chat_id}


async def close_entity_thread(
    pool: asyncpg.Pool,
    bot: Bot,
    entity_type: str,
    entity_id: int,
    owner_id: int | None = None,
) -> bool:
    """Close forum topic and archive the thread record."""
    row = await pool.fetchrow(
        """
        SELECT t.id, t.tg_thread_id, n.tg_chat_id
        FROM bm_node_threads t
        JOIN bm_telegram_nodes n ON n.id = t.node_id
        WHERE t.entity_type=$1 AND t.entity_id=$2 AND t.status='open'
          AND ($3::BIGINT IS NULL OR n.owner_id=$3)
        ORDER BY t.created_at DESC LIMIT 1
        """,
        entity_type, entity_id, owner_id,
    )
    if not row:
        return False

    try:
        await bot.close_forum_topic(
            chat_id=row["tg_chat_id"],
            message_thread_id=row["tg_thread_id"],
        )
    except Exception as exc:
        log.warning("nodes_engine: close_forum_topic failed: %s", exc)

    await pool.execute(
        "UPDATE bm_node_threads SET status='archived' WHERE id=$1",
        row["id"],
    )
    log.info("nodes_engine: thread archived entity=%s/%d", entity_type, entity_id)
    return True


# ── Messaging ─────────────────────────────────────────────────────────────────

async def log_to_entity_thread(
    pool: asyncpg.Pool,
    bot: Bot,
    entity_type: str,
    entity_id: int,
    text: str,
    owner_id: int | None = None,
) -> bool:
    """Send HTML-formatted message to entity's forum thread.

    Returns True on success.
    """
    row = await pool.fetchrow(
        """
        SELECT t.tg_thread_id, n.tg_chat_id
        FROM bm_node_threads t
        JOIN bm_telegram_nodes n ON n.id = t.node_id
        WHERE t.entity_type=$1 AND t.entity_id=$2 AND t.status='open'
          AND ($3::BIGINT IS NULL OR n.owner_id=$3)
        ORDER BY t.created_at DESC LIMIT 1
        """,
        entity_type, entity_id, owner_id,
    )
    if not row:
        log.debug("nodes_engine: no open thread for %s/%d", entity_type, entity_id)
        return False

    try:
        await bot.send_message(
            chat_id=row["tg_chat_id"],
            text=text,
            message_thread_id=row["tg_thread_id"],
            parse_mode="HTML",
        )
        return True
    except Exception as exc:
        log.error("nodes_engine: log_to_thread failed %s/%d: %s", entity_type, entity_id, exc)
        return False


# ── STRIKE mass operations ────────────────────────────────────────────────────

async def strike_bulk_create_threads(
    pool: asyncpg.Pool,
    bot: Bot,
    owner_id: int,
    entity_type: str,
    entities: list[dict],
) -> tuple[list[dict], int]:
    """Batch-provision forum threads for many entities with strict rate-limiting.

    entities = [{"id": int, "name": str}, ...]
    Rate: ≤2.5 requests/sec (Telegram forum topic creation limit).
    Returns (created_threads, error_count).
    """
    node_type = _ENTITY_TO_NODE.get(entity_type, entity_type)
    node = await pool.fetchrow(
        """SELECT * FROM bm_telegram_nodes
           WHERE owner_id=$1 AND node_type=$2 AND is_active=TRUE ORDER BY id LIMIT 1""",
        owner_id, node_type,
    )
    if not node:
        log.error(
            "nodes_engine: STRIKE bulk_create: no workspace owner=%d type=%s",
            owner_id, entity_type,
        )
        return [], len(entities)

    created: list[dict] = []
    errors = 0
    total = len(entities)

    for idx, entity in enumerate(entities):
        eid = entity["id"]
        name = entity.get("name") or f"{ENTITY_LABELS.get(entity_type, entity_type)} #{eid}"

        result = await provision_thread_for_entity(
            pool=pool,
            bot=bot,
            owner_id=owner_id,
            entity_type=entity_type,
            entity_id=eid,
            topic_name=name,
            tg_chat_id=node["tg_chat_id"],
        )

        if result:
            created.append(result)
        else:
            errors += 1
            log.warning(
                "nodes_engine: STRIKE bulk_create entity=%s/%d failed (%d/%d)",
                entity_type, eid, idx + 1, total,
            )

        if idx < total - 1:
            await asyncio.sleep(_FORUM_DELAY)

    log.info(
        "nodes_engine: STRIKE bulk_create done: %d ok / %d errors / %d total",
        len(created), errors, total,
    )
    return created, errors


async def strike_broadcast_to_threads(
    pool: asyncpg.Pool,
    bot: Bot,
    owner_id: int,
    entity_type: str,
    entity_ids: list[int],
    message: str,
) -> dict[str, int]:
    """Blast alert message to many entity threads using chunked concurrency.

    Returns {"sent": N, "failed": M}.
    """
    rows = await pool.fetch(
        """
        SELECT t.tg_thread_id, n.tg_chat_id
        FROM bm_node_threads t
        JOIN bm_telegram_nodes n ON n.id = t.node_id
        WHERE n.owner_id=$1
          AND t.entity_type=$2
          AND t.entity_id = ANY($3::BIGINT[])
          AND t.status = 'open'
        """,
        owner_id, entity_type, entity_ids,
    )

    if not rows:
        return {"sent": 0, "failed": len(entity_ids)}

    sent = 0
    failed = 0

    chunks = [rows[i:i + _BROADCAST_CHUNK] for i in range(0, len(rows), _BROADCAST_CHUNK)]

    for chunk_idx, chunk in enumerate(chunks):
        tasks = [
            bot.send_message(
                chat_id=r["tg_chat_id"],
                text=message,
                message_thread_id=r["tg_thread_id"],
                parse_mode="HTML",
            )
            for r in chunk
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                failed += 1
                log.debug("nodes_engine: broadcast send error: %s", r)
            else:
                sent += 1

        if chunk_idx < len(chunks) - 1:
            await asyncio.sleep(_BROADCAST_CHUNK_DELAY)

    log.info("nodes_engine: STRIKE broadcast done: %d sent / %d failed", sent, failed)
    return {"sent": sent, "failed": failed}


# ── Reverse command routing ───────────────────────────────────────────────────

async def route_node_command(
    pool: asyncpg.Pool,
    tg_chat_id: int,
    message_thread_id: int,
    text: str,
    from_user_id: int,
) -> dict[str, Any] | None:
    """Resolve which entity a forum thread belongs to and parse inline commands.

    Intercepts messages in Node threads from managers/admins.
    Returns:
        {entity_type, entity_id, owner_id, node_type, command, args, raw_text}
    or None if thread is not a registered Node thread.
    """
    row = await pool.fetchrow(
        """
        SELECT t.entity_type, t.entity_id, t.id AS thread_db_id,
               n.owner_id, n.node_type, n.tg_chat_id
        FROM bm_node_threads t
        JOIN bm_telegram_nodes n ON n.id = t.node_id
        WHERE n.tg_chat_id = $1 AND t.tg_thread_id = $2 AND t.status = 'open'
        """,
        tg_chat_id, message_thread_id,
    )
    if not row:
        return None

    command: str | None = None
    args: str = ""
    raw = (text or "").strip()

    if raw.startswith("/"):
        parts = raw.split(None, 1)
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

    return {
        "entity_type":   row["entity_type"],
        "entity_id":     row["entity_id"],
        "owner_id":      row["owner_id"],
        "node_type":     row["node_type"],
        "thread_db_id":  row["thread_db_id"],
        "command":       command,
        "args":          args,
        "raw_text":      raw,
        "from_user_id":  from_user_id,
    }


# ── DB read helpers ───────────────────────────────────────────────────────────

async def get_workspaces(
    pool: asyncpg.Pool,
    owner_id: int,
    node_type: str | None = None,
) -> list[dict]:
    if node_type:
        rows = await pool.fetch(
            "SELECT * FROM bm_telegram_nodes WHERE owner_id=$1 AND node_type=$2 AND is_active=TRUE ORDER BY id",
            owner_id, node_type,
        )
    else:
        rows = await pool.fetch(
            "SELECT * FROM bm_telegram_nodes WHERE owner_id=$1 AND is_active=TRUE ORDER BY id",
            owner_id,
        )
    return [dict(r) for r in rows]


async def get_node_by_id(
    pool: asyncpg.Pool,
    node_id: int,
    owner_id: int,
) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM bm_telegram_nodes WHERE id=$1 AND owner_id=$2",
        node_id, owner_id,
    )
    return dict(row) if row else None


async def get_threads(
    pool: asyncpg.Pool,
    node_id: int,
    status: str = "open",
    limit: int = 50,
) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM bm_node_threads WHERE node_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3",
        node_id, status, limit,
    )
    return [dict(r) for r in rows]


async def get_thread_stats(pool: asyncpg.Pool, node_id: int) -> dict:
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'open')     AS open_count,
            COUNT(*) FILTER (WHERE status = 'archived') AS archived_count,
            COUNT(*)                                     AS total_count
        FROM bm_node_threads WHERE node_id = $1
        """,
        node_id,
    )
    return dict(row) if row else {"open_count": 0, "archived_count": 0, "total_count": 0}


# ── Report builder ────────────────────────────────────────────────────────────

def build_status_report(
    entity_type: str,
    entity_id: int,
    status: str,
    details: dict[str, Any],
) -> str:
    """Build HTML-formatted infrastructure status report for entity thread."""
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    label = ENTITY_LABELS.get(entity_type, entity_type)

    lines: list[str] = [
        f"<b>{html.escape(label)} #{entity_id}</b>  •  <i>{now_str}</i>",
        "",
        f"Статус: <b>{html.escape(status)}</b>",
    ]
    for key, val in details.items():
        lines.append(f"• {html.escape(str(key))}: <code>{html.escape(str(val))}</code>")

    return "\n".join(lines)
