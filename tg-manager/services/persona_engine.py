"""Persona Engine — AI personas with persistent memory and consistent behavior."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import asyncpg

log = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class PersonaProfile:
    persona_id: int
    account_id: int
    name: str
    bio: str
    age: int
    interests: list[str]
    speech_style: str
    tone: str
    niche: str
    backstory: str
    created_at: datetime


# ── DB helpers ────────────────────────────────────────────────────────────────


async def create_persona(
    pool: asyncpg.Pool,
    account_id: int,
    owner_id: int,
    name: str,
    bio: str,
    interests: list[str],
    niche: str,
    speech_style: str = "neutral",
    backstory: str = "",
) -> int:
    """Insert a new persona profile and return its id."""
    persona_id: int = await pool.fetchval(
        """
        INSERT INTO persona_profiles
               (account_id, owner_id, persona_name, bio, interests, niche,
                speech_style, backstory)
        VALUES ($1, $2, $3, $4, $5::TEXT[], $6, $7, $8)
        ON CONFLICT (account_id) DO UPDATE
            SET persona_name = EXCLUDED.persona_name,
                bio          = EXCLUDED.bio,
                interests    = EXCLUDED.interests,
                niche        = EXCLUDED.niche,
                speech_style = EXCLUDED.speech_style,
                backstory    = EXCLUDED.backstory,
                updated_at   = NOW()
        RETURNING id
        """,
        account_id,
        owner_id,
        name,
        bio,
        interests,
        niche,
        speech_style,
        backstory,
    )
    return persona_id


async def get_persona(pool: asyncpg.Pool, account_id: int) -> PersonaProfile | None:
    """Return the PersonaProfile for a given account_id, or None."""
    row = await pool.fetchrow(
        "SELECT * FROM persona_profiles WHERE account_id = $1 AND is_active = TRUE",
        account_id,
    )
    if not row:
        return None
    return PersonaProfile(
        persona_id=row["id"],
        account_id=row["account_id"],
        name=row["persona_name"],
        bio=row["bio"] or "",
        age=row["age"] or 25,
        interests=list(row["interests"] or []),
        speech_style=row["speech_style"] or "neutral",
        tone=row["tone"] or "positive",
        niche=row["niche"] or "",
        backstory=row["backstory"] or "",
        created_at=row["created_at"],
    )


async def get_persona_by_id(pool: asyncpg.Pool, persona_id: int) -> PersonaProfile | None:
    """Return the PersonaProfile for a given persona id, or None."""
    row = await pool.fetchrow(
        "SELECT * FROM persona_profiles WHERE id = $1",
        persona_id,
    )
    if not row:
        return None
    return PersonaProfile(
        persona_id=row["id"],
        account_id=row["account_id"],
        name=row["persona_name"],
        bio=row["bio"] or "",
        age=row["age"] or 25,
        interests=list(row["interests"] or []),
        speech_style=row["speech_style"] or "neutral",
        tone=row["tone"] or "positive",
        niche=row["niche"] or "",
        backstory=row["backstory"] or "",
        created_at=row["created_at"],
    )


# ── Prompt builder ────────────────────────────────────────────────────────────


def build_persona_system_prompt(persona: PersonaProfile) -> str:
    """Build an OpenAI-compatible system prompt that locks the model into the persona."""
    interests_str = ", ".join(persona.interests) if persona.interests else "общие темы"
    bio_text = persona.bio.strip()
    if bio_text and not bio_text.endswith("."):
        bio_text += "."
    parts = [
        f"Ты {persona.name}, {persona.age} лет.",
        bio_text,
        f"Твои интересы: {interests_str}.",
        f"Стиль речи: {persona.speech_style}.",
    ]
    if persona.niche:
        parts.append(f"Ниша: {persona.niche}.")
    if persona.backstory:
        parts.append(persona.backstory.strip())
    parts.append("Отвечай всегда в этом образе.")
    return " ".join(p for p in parts if p)


# ── AI generation ─────────────────────────────────────────────────────────────


async def generate_persona_response(
    pool: asyncpg.Pool,
    persona: PersonaProfile,
    context: str,
    ai_provider,
) -> str:
    """Generate an AI response in persona character using the given provider."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return "⚠️ Библиотека openai не установлена."

    system_prompt = build_persona_system_prompt(persona)

    client = AsyncOpenAI(
        api_key=ai_provider.api_key,
        base_url=ai_provider.base_url,
        timeout=25.0,
    )
    model = ai_provider.models[0] if ai_provider.models else "gpt-3.5-turbo"
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": context},
            ],
            max_tokens=512,
        )
        return response.choices[0].message.content or ""
    except Exception as e:
        log.warning("persona_engine generate_persona_response error: %s", e)
        return f"⚠️ Ошибка генерации: {e}"


# ── Memory ────────────────────────────────────────────────────────────────────


async def record_persona_memory(
    pool: asyncpg.Pool,
    persona_id: int,
    event_type: str,
    content: str,
    entity: str,
    owner_id: int,
    sentiment: str = "neutral",
) -> None:
    """Record an action the persona performed."""
    try:
        await pool.execute(
            """
            INSERT INTO persona_memory
                   (persona_id, owner_id, event_type, content, entity, sentiment)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            persona_id,
            owner_id,
            event_type,
            content,
            entity,
            sentiment,
        )
    except Exception as e:
        log.warning("persona_engine record_persona_memory error: %s", e)


async def get_persona_memory(
    pool: asyncpg.Pool,
    persona_id: int,
    limit: int = 50,
) -> list[asyncpg.Record]:
    """Return the last N memory entries for a persona."""
    return await pool.fetch(
        """
        SELECT * FROM persona_memory
        WHERE persona_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        persona_id,
        limit,
    )
