"""Niche Group Searcher — автопоиск Telegram-групп по нише.

Поток:
  1. generate_keywords()  → AI или rule-based ключевые слова
  2. search_groups()      → SearchRequest по каждому keyword
  3. Фильтрация дубликатов, минимум участников
  4. Возврат списка join-ссылок для bulk_join
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import asyncpg

log = logging.getLogger(__name__)

# ── Ключевые слова ────────────────────────────────────────────────────────────

async def generate_keywords(
    description: str,
    ai_provider: Any | None = None,
) -> list[str]:
    """Сгенерировать поисковые запросы для ниши.

    Сначала пробует AI (если ai_provider задан), иначе rule-based.
    """
    if ai_provider is not None:
        try:
            return await _keywords_via_ai(description, ai_provider)
        except Exception as exc:
            log.warning("niche_searcher: AI keyword gen failed: %s", exc)
    return _keywords_rule_based(description)


async def _keywords_via_ai(description: str, ai_provider: Any) -> list[str]:
    import openai  # type: ignore

    client = openai.AsyncOpenAI(
        api_key=ai_provider.api_key,
        base_url=ai_provider.base_url,
    )
    model = ai_provider.models[0] if ai_provider.models else "gpt-3.5-turbo"

    prompt = (
        "Ты помогаешь найти Telegram-группы и чаты по заданной нише.\n"
        f"Цель пользователя: {description}\n\n"
        "Сгенерируй 10 поисковых запросов для поиска групп в Telegram.\n"
        "Правила:\n"
        "- Включай запросы на русском И английском\n"
        "- Включай синонимы, смежные темы, региональные варианты\n"
        "- Запросы должны быть конкретными (3-5 слов)\n"
        "- Верни ТОЛЬКО список запросов, каждый на новой строке, без нумерации"
    )

    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.7,
        ),
        timeout=20.0,
    )
    text = (resp.choices[0].message.content or "").strip()
    raw = [line.strip().lstrip("-•*0123456789.) ") for line in text.splitlines()]
    keywords = [k for k in raw if 2 < len(k) < 80]
    return keywords[:12]


def _keywords_rule_based(description: str) -> list[str]:
    """Fallback: извлекаем слова из описания и дедублируем."""
    words = re.findall(r'[а-яёА-ЯЁa-zA-Z]{3,}', description)
    _STOP = {
        'нише', 'нишу', 'нишей', 'ниша', 'подписчик', 'подписчиков',
        'subscriber', 'subscribers', 'channel', 'бот', 'наш', 'для',
        'канал', 'группа', 'набрать', 'вырасти', 'цель', 'хочу', 'хочем',
        'нужно', 'нужен', 'grow', 'member', 'members',
    }
    seen: list[str] = []
    for w in words:
        if w.lower() not in _STOP and w not in seen:
            seen.append(w)
    return seen[:8]


# ── Поиск групп в Telegram ────────────────────────────────────────────────────

async def search_niche_groups(
    session_string: str,
    keywords: list[str],
    min_members: int = 200,
    max_per_keyword: int = 15,
    exclude_ids: set[int] | None = None,
    _acc: dict | None = None,
) -> list[dict[str, Any]]:
    """Поиск публичных мегагрупп в Telegram по списку ключевых слов.

    Возвращает дедуплицированный список:
      {id, title, username, access_hash, members, join_ref}
    """
    from services.account_manager import _make_client  # type: ignore

    client = _make_client(session_string, _acc)
    found: dict[int, dict] = {}

    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)

        for kw in keywords:
            try:
                groups = await _search_one_keyword(
                    client, kw, max_per_keyword, min_members
                )
                for g in groups:
                    if exclude_ids and g["id"] in exclude_ids:
                        continue
                    if g["id"] not in found:
                        found[g["id"]] = g
                # Небольшая пауза между запросами
                await asyncio.sleep(1.5)
            except Exception as exc:
                log.warning("niche_searcher: keyword=%r failed: %s", kw, exc)
                continue

    except Exception as exc:
        log.warning("niche_searcher: connect/search failed: %s", exc)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    results = sorted(found.values(), key=lambda g: g["members"], reverse=True)
    log.info(
        "niche_searcher: %d keywords → %d unique groups (min_members=%d)",
        len(keywords), len(results), min_members,
    )
    return results


async def _search_one_keyword(
    client: Any,
    keyword: str,
    limit: int,
    min_members: int,
) -> list[dict[str, Any]]:
    from telethon.tl.functions.contacts import SearchRequest  # type: ignore
    from telethon.tl.types import Channel  # type: ignore

    result = await asyncio.wait_for(
        client(SearchRequest(q=keyword, limit=limit)),
        timeout=12.0,
    )

    groups = []
    for chat in getattr(result, "chats", []):
        if not isinstance(chat, Channel):
            continue
        if not getattr(chat, "megagroup", False):
            continue  # только мегагруппы, не каналы
        if getattr(chat, "restricted", False) or getattr(chat, "scam", False):
            continue
        members = getattr(chat, "participants_count", 0) or 0
        if members < min_members:
            continue
        username = getattr(chat, "username", "") or ""
        if not username:
            continue  # только публичные (есть username)
        groups.append({
            "id":          int(chat.id),
            "title":       getattr(chat, "title", "") or "",
            "username":    username,
            "access_hash": getattr(chat, "access_hash", 0) or 0,
            "members":     members,
            "join_ref":    f"@{username}",
        })
    return groups


# ── Утилита: уже вступленные группы ──────────────────────────────────────────

async def get_joined_group_ids(
    pool: asyncpg.Pool,
    owner_id: int,
    acc_ids: list[int],
) -> set[int]:
    """Вернуть set channel_id групп которые уже есть в managed_channels."""
    if not acc_ids:
        return set()
    try:
        rows = await pool.fetch(
            "SELECT channel_id FROM managed_channels "
            "WHERE owner_id=$1 AND acc_id = ANY($2::bigint[]) "
            "AND type IN ('megagroup', 'supergroup', 'group', 'chat')",
            owner_id, acc_ids,
        )
        return {int(r["channel_id"]) for r in rows}
    except Exception as exc:
        log.warning("niche_searcher: get_joined_group_ids failed: %s", exc)
        return set()
