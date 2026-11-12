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
    tone: str = "positive",
    backstory: str = "",
) -> int:
    """Insert a new persona profile and return its id."""
    persona_id: int = await pool.fetchval(
        """
        INSERT INTO persona_profiles
               (account_id, owner_id, persona_name, bio, interests, niche,
                speech_style, tone, backstory)
        VALUES ($1, $2, $3, $4, $5::TEXT[], $6, $7, $8, $9)
        ON CONFLICT (account_id) DO UPDATE
            SET persona_name = EXCLUDED.persona_name,
                bio          = EXCLUDED.bio,
                interests    = EXCLUDED.interests,
                niche        = EXCLUDED.niche,
                speech_style = EXCLUDED.speech_style,
                tone         = EXCLUDED.tone,
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
        tone,
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


# Стили и тон — закрытые списки с подписями по-русски. Модель получала ключ
# («Стиль речи: casual.»), то есть английское слово без смысла для неё в
# русском промпте; теперь — описание того, КАК писать.
STYLE_LABELS = {
    "casual": "разговорный",
    "formal": "формальный",
    "expert": "экспертный",
    "friendly": "дружелюбный",
    "sarcastic": "саркастичный",
    "neutral": "нейтральный",
}
STYLE_PROMPTS = {
    "casual": "Пишешь просто и разговорно, как в переписке с приятелем, иногда строчными.",
    "formal": "Пишешь вежливо и сдержанно, полными предложениями, без сленга.",
    "expert": "Пишешь как специалист: по делу, с конкретикой и цифрами, без воды.",
    "friendly": "Пишешь тепло и доброжелательно, поддерживаешь собеседника.",
    "sarcastic": "Пишешь с иронией и лёгким сарказмом, но без грубости.",
    "neutral": "Пишешь спокойно и естественно, без лишних эмоций.",
}
TONE_LABELS = {
    "positive": "позитивный",
    "neutral": "нейтральный",
    "skeptical": "скептичный",
    "enthusiastic": "восторженный",
}
TONE_PROMPTS = {
    "positive": "Настрой в целом доброжелательный.",
    "neutral": "Настрой ровный.",
    "skeptical": "Ко всему относишься с долей сомнения, любишь уточнить.",
    "enthusiastic": "Легко загораешься темой, говоришь с энтузиазмом.",
}


def build_persona_system_prompt(persona: PersonaProfile) -> str:
    """Системный промпт, который держит модель в образе персоны.

    Внешний текст (биография и предыстория — их пишет владелец) идёт в промпт
    как описание характера; правило «ты живой человек, не ИИ» стоит в конце,
    чтобы его не перебила предыстория.
    """
    interests_str = ", ".join(persona.interests) if persona.interests else "общие темы"
    bio_text = (persona.bio or "").strip()
    if bio_text and not bio_text.endswith((".", "!", "?")):
        bio_text += "."
    parts = [
        f"Ты {persona.name}, {persona.age} лет.",
        bio_text,
        f"Твои интересы: {interests_str}.",
        STYLE_PROMPTS.get(persona.speech_style, STYLE_PROMPTS["neutral"]),
        TONE_PROMPTS.get(persona.tone or "", ""),
    ]
    if persona.niche:
        parts.append(f"Разбираешься в теме: {persona.niche}.")
    if persona.backstory:
        parts.append(persona.backstory.strip())
    parts.append("Отвечай всегда в этом образе, как живой человек; "
                 "не упоминай, что ты ИИ или бот.")
    return " ".join(p for p in parts if p)


async def persona_prompt_for_account(pool: asyncpg.Pool, account_id: int) -> str:
    """Промпт персоны, привязанной к аккаунту, или '' — если персоны нет,
    она выключена или база не ответила (fail-open: без персоны действие
    идёт с обычным голосом, а не падает)."""
    try:
        p = await get_persona(pool, int(account_id))
    except Exception as exc:
        log.warning("persona_prompt_for_account acc=%s: %s", account_id, exc)
        return ""
    return build_persona_system_prompt(p) if p else ""


# ── AI generation ─────────────────────────────────────────────────────────────


async def generate_persona_response(
    pool: asyncpg.Pool,
    persona: PersonaProfile,
    context: str,
    ai_provider,
) -> str:
    """Ответ в образе персоны через единый шлюз (лимиты, учёт, запасные модели)."""
    from services import ai_gateway

    system_prompt = build_persona_system_prompt(persona)
    r = await ai_gateway.chat(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": context}],
        task=ai_gateway.TASK_ASSISTANT,
        owner_id=getattr(persona, "owner_id", None),
        max_tokens=512,
        prefer_provider=getattr(ai_provider, "name", "") or "",
    )
    if r.text:
        return r.text
    log.warning("persona_engine generate_persona_response: %s", "; ".join(r.errors)[:300])
    return "⚠️ Ошибка генерации: ИИ не ответил"


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
