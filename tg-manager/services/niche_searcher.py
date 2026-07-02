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
        "Сгенерируй 12 поисковых запросов для поиска групп в Telegram.\n"
        "Правила:\n"
        "- Включай запросы на русском И английском\n"
        "- Включай синонимы, смежные темы, региональные варианты\n"
        "- Добавляй городские/региональные варианты (Москва, СПб, Киев, Минск, Алматы)\n"
        "- Запросы должны быть конкретными (2-4 слова)\n"
        "- Если ниша связана с наркотиками, оружием, секс-услугами или другим "
        "незаконным/запрещённым контентом — откажись генерировать запросы и верни "
        "пустой список\n"
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
    min_members: int = 50,
    max_per_keyword: int = 20,
    exclude_ids: set[int] | None = None,
    _acc: dict | None = None,
) -> list[dict[str, Any]]:
    """Поиск публичных групп в Telegram по списку ключевых слов.

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
                await asyncio.sleep(1.5)
            except Exception as exc:
                log.warning("niche_searcher: keyword=%r failed: %s", kw, exc)
                continue

        # Если ничего не нашли → повторяем с relaxed min_members=0
        if not found:
            log.info("niche_searcher: no results with min=%d, retrying with min=0", min_members)
            for kw in keywords[:6]:
                try:
                    groups = await _search_one_keyword(client, kw, max_per_keyword, 0)
                    for g in groups:
                        if exclude_ids and g["id"] in exclude_ids:
                            continue
                        if g["id"] not in found:
                            found[g["id"]] = g
                    await asyncio.sleep(1.5)
                except Exception as exc:
                    log.warning("niche_searcher relaxed: keyword=%r failed: %s", kw, exc)
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
        "niche_searcher: %d keywords → %d unique groups",
        len(keywords), len(results),
    )
    return results


async def _search_one_keyword(
    client: Any,
    keyword: str,
    limit: int,
    min_members: int,
) -> list[dict[str, Any]]:
    from telethon.tl.functions.contacts import SearchRequest  # type: ignore
    from telethon.tl.types import Channel, Chat  # type: ignore

    result = await asyncio.wait_for(
        client(SearchRequest(q=keyword, limit=limit)),
        timeout=12.0,
    )

    groups = []
    for chat in getattr(result, "chats", []):
        # Принимаем Channel (supergroup/megagroup) и базовые Chat-группы
        is_channel = isinstance(chat, Channel)
        is_basic   = isinstance(chat, Chat)
        if not (is_channel or is_basic):
            continue
        # Исключаем каналы-вещалки (broadcast=True), но НЕ megagroup=False
        if is_channel and getattr(chat, "broadcast", False):
            continue
        if getattr(chat, "restricted", False) or getattr(chat, "scam", False):
            continue
        members = getattr(chat, "participants_count", 0) or 0
        # Фильтруем по участникам только если значение известно (> 0)
        if members > 0 and members < min_members:
            continue
        # Предпочитаем группы с username, но берём и без (invite link)
        username = getattr(chat, "username", "") or ""
        join_ref = f"@{username}" if username else None
        if not join_ref:
            continue  # без username bulk_join не умеет вступать
        groups.append({
            "id":          int(chat.id),
            "title":       getattr(chat, "title", "") or "",
            "username":    username,
            "access_hash": getattr(chat, "access_hash", 0) or 0,
            "members":     members,
            "join_ref":    join_ref,
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
