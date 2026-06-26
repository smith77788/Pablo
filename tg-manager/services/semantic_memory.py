"""Semantic Memory CRM — per-user per-bot conversational memory."""

from __future__ import annotations

import json
import logging
from html import escape as html_escape
from typing import Any

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

# ── Core interaction recording ──────────────────────────────────────────────


async def record_interaction(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    role: str,
    text: str,
    metadata: dict | None = None,
) -> int:
    """Insert one interaction into bot_user_memory.

    Returns the new row id.
    role must be 'user' or 'bot'.
    """
    if role not in ("user", "bot"):
        raise ValueError(f"role must be 'user' or 'bot', got {role!r}")
    clean_text = (text or "").strip()
    if not clean_text:
        return 0
    row = await pool.fetchrow(
        """
        INSERT INTO bot_user_memory(bot_id, user_id, role, text, metadata)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        bot_id,
        user_id,
        role,
        clean_text[:8000],
        json.dumps(metadata or {}),
    )
    return row["id"] if row else 0


# ── Context retrieval ────────────────────────────────────────────────────────


async def get_context(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    max_messages: int = 20,
) -> list[dict[str, Any]]:
    """Return the last *max_messages* interactions for AI context.

    Result format: list of {'role': 'user'|'bot', 'text': str, 'created_at': datetime}
    Ordered oldest → newest (ready for chat-completion messages array).
    Memory window is capped by the per-bot max_history_days setting (default 90 days).
    """
    settings = await _get_settings(pool, bot_id)
    if not settings.get("enabled", True):
        return []
    max_days = settings.get("max_history_days", 90)
    rows = await pool.fetch(
        """
        SELECT role, text, created_at
        FROM bot_user_memory
        WHERE bot_id = $1
          AND user_id = $2
          AND created_at >= NOW() - ($3 * INTERVAL '1 day')
        ORDER BY created_at DESC
        LIMIT $4
        """,
        bot_id,
        user_id,
        max_days,
        max_messages,
    )
    # Reverse to chronological order
    return [
        {"role": r["role"], "text": r["text"], "created_at": r["created_at"]}
        for r in reversed(rows)
    ]


# ── Fact extraction ──────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = (
    "Ты — CRM-агент. Из переписки пользователя с чат-ботом извлеки ключевые факты.\n"
    "Верни JSON-объект (без лишнего текста) со следующими возможными полями:\n"
    "  name        — имя пользователя (строка)\n"
    "  interests   — интересы/темы (строка)\n"
    "  purchases   — что покупал / хочет купить (строка)\n"
    "  pain_points — боли, проблемы (строка)\n"
    "  location    — город/страна (строка)\n"
    "  language    — язык общения (строка)\n"
    "  goals       — цели и намерения (строка)\n"
    "Включай только те поля, которые достоверно следуют из текста. "
    "Если данных нет — вернуть пустой объект {}."
)


async def extract_facts(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    conversation_text: str,
    ai_provider: Any,
) -> dict[str, str]:
    """Call AI to extract key facts from *conversation_text* and persist to bot_user_facts.

    Returns the extracted facts dict (may be empty if AI fails or no facts found).
    ai_provider is an AiProvider dataclass from services.ai_providers.
    """
    if not conversation_text or not ai_provider:
        return {}

    payload = {
        "model": ai_provider.models[0],
        "messages": [
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": conversation_text[:6000]},
        ],
        "max_tokens": 512,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {ai_provider.api_key}",
        "Content-Type": "application/json",
    }

    facts: dict[str, str] = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ai_provider.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "semantic_memory.extract_facts: provider=%s status=%d",
                        ai_provider.name,
                        resp.status,
                    )
                    return {}
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"].strip()
                # Strip possible markdown fences
                if raw.startswith("```"):
                    raw = raw.split("```", 2)[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.rsplit("```", 1)[0]
                facts = json.loads(raw.strip())
                if not isinstance(facts, dict):
                    facts = {}
    except Exception as exc:
        log.warning("semantic_memory.extract_facts: %s", exc)
        return {}

    # Persist each extracted fact (upsert)
    for key, value in facts.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key_clean = key.strip()[:64]
        value_clean = value.strip()[:2000]
        if not key_clean or not value_clean:
            continue
        try:
            await pool.execute(
                """
                INSERT INTO bot_user_facts(bot_id, user_id, fact_key, fact_value, confidence)
                VALUES ($1, $2, $3, $4, 0.9)
                ON CONFLICT (bot_id, user_id, fact_key)
                DO UPDATE SET fact_value = EXCLUDED.fact_value,
                              confidence = 0.9,
                              updated_at = NOW()
                """,
                bot_id,
                user_id,
                key_clean,
                value_clean,
            )
        except Exception as exc:
            log.warning(
                "semantic_memory.extract_facts: persist failed key=%s: %s", key, exc
            )

    return facts


# ── Memory prompt builder ────────────────────────────────────────────────────

_FACT_LABELS: dict[str, str] = {
    "name": "Имя",
    "interests": "Интересы",
    "purchases": "Покупки",
    "pain_points": "Боли",
    "location": "Локация",
    "language": "Язык",
    "goals": "Цели",
}


async def build_memory_prompt(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
) -> str:
    """Build a compact context string for insertion into the AI system prompt.

    Returns empty string if no facts and no recent messages.
    """
    settings = await _get_settings(pool, bot_id)
    if not settings.get("enabled", True):
        return ""

    # Fetch stored facts
    fact_rows = await pool.fetch(
        """
        SELECT fact_key, fact_value
        FROM bot_user_facts
        WHERE bot_id = $1 AND user_id = $2
        ORDER BY fact_key
        """,
        bot_id,
        user_id,
    )
    facts = {r["fact_key"]: r["fact_value"] for r in fact_rows}

    if not facts:
        return ""

    parts: list[str] = []
    for key, value in facts.items():
        label = _FACT_LABELS.get(key, key)
        parts.append(f"{label}: {value}")

    header = "Контекст пользователя:"
    return header + " " + "; ".join(parts) + "."


# ── GDPR cleanup ─────────────────────────────────────────────────────────────


async def clear_user_memory(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
) -> tuple[int, int]:
    """Delete all memory and facts for one user of one bot.

    Returns (deleted_messages, deleted_facts).
    """
    res_mem = await pool.execute(
        "DELETE FROM bot_user_memory WHERE bot_id=$1 AND user_id=$2",
        bot_id,
        user_id,
    )
    res_facts = await pool.execute(
        "DELETE FROM bot_user_facts WHERE bot_id=$1 AND user_id=$2",
        bot_id,
        user_id,
    )
    deleted_mem = int((res_mem or "DELETE 0").split()[-1])
    deleted_facts = int((res_facts or "DELETE 0").split()[-1])
    return deleted_mem, deleted_facts


# ── Settings helpers ─────────────────────────────────────────────────────────


async def _get_settings(pool: asyncpg.Pool, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT enabled, max_history_days, auto_extract_facts FROM memory_settings WHERE bot_id=$1",
        bot_id,
    )
    if row:
        return dict(row)
    return {"enabled": True, "max_history_days": 90, "auto_extract_facts": True}


async def get_settings(pool: asyncpg.Pool, bot_id: int) -> dict:
    """Public wrapper — returns memory settings for given bot."""
    return await _get_settings(pool, bot_id)


async def upsert_settings(
    pool: asyncpg.Pool,
    bot_id: int,
    *,
    enabled: bool | None = None,
    max_history_days: int | None = None,
    auto_extract_facts: bool | None = None,
) -> None:
    """Create or update memory settings for a bot."""
    current = await _get_settings(pool, bot_id)
    new_enabled = enabled if enabled is not None else current["enabled"]
    new_days = (
        max_history_days
        if max_history_days is not None
        else current["max_history_days"]
    )
    new_extract = (
        auto_extract_facts
        if auto_extract_facts is not None
        else current["auto_extract_facts"]
    )
    await pool.execute(
        """
        INSERT INTO memory_settings(bot_id, enabled, max_history_days, auto_extract_facts)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (bot_id)
        DO UPDATE SET enabled = EXCLUDED.enabled,
                      max_history_days = EXCLUDED.max_history_days,
                      auto_extract_facts = EXCLUDED.auto_extract_facts,
                      updated_at = NOW()
        """,
        bot_id,
        new_enabled,
        new_days,
        new_extract,
    )


# ── Statistics helper ─────────────────────────────────────────────────────────


async def get_stats(pool: asyncpg.Pool, bot_id: int) -> dict:
    """Return aggregate memory stats for a bot.

    Returns dict with keys: total_users, total_messages, avg_messages_per_user, total_facts.
    """
    row = await pool.fetchrow(
        """
        SELECT
            COUNT(DISTINCT user_id)::int AS total_users,
            COUNT(*)::int AS total_messages,
            ROUND(AVG(cnt), 1) AS avg_messages_per_user
        FROM (
            SELECT user_id, COUNT(*) AS cnt
            FROM bot_user_memory
            WHERE bot_id = $1
            GROUP BY user_id
        ) sub
        """,
        bot_id,
    )
    total_facts = await pool.fetchval(
        "SELECT COUNT(*)::int FROM bot_user_facts WHERE bot_id=$1",
        bot_id,
    )
    if row:
        return {
            "total_users": row["total_users"] or 0,
            "total_messages": row["total_messages"] or 0,
            "avg_messages_per_user": float(row["avg_messages_per_user"] or 0),
            "total_facts": total_facts or 0,
        }
    return {
        "total_users": 0,
        "total_messages": 0,
        "avg_messages_per_user": 0.0,
        "total_facts": 0,
    }


# ── User history for CRM view ─────────────────────────────────────────────────


async def get_user_history(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
    limit: int = 50,
) -> list[dict]:
    """Return message history for one user of one bot (newest first)."""
    rows = await pool.fetch(
        """
        SELECT id, role, text, metadata, created_at
        FROM bot_user_memory
        WHERE bot_id=$1 AND user_id=$2
        ORDER BY created_at DESC
        LIMIT $3
        """,
        bot_id,
        user_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_user_facts(
    pool: asyncpg.Pool,
    bot_id: int,
    user_id: int,
) -> list[dict]:
    """Return all extracted facts for one user of one bot."""
    rows = await pool.fetch(
        """
        SELECT fact_key, fact_value, confidence, updated_at
        FROM bot_user_facts
        WHERE bot_id=$1 AND user_id=$2
        ORDER BY fact_key
        """,
        bot_id,
        user_id,
    )
    return [dict(r) for r in rows]
