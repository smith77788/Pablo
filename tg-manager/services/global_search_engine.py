"""Global Search — глобальный поиск публичных каналов/групп/пользователей.

Раздел 12 (Специальные модули → Global Search) и раздел 5 (Сбор аудитории →
по ключевым словам) из паритета Telegram Expert. Использует Telethon-примитив
``contacts.SearchRequest`` (тот же вызов, что глобальный поиск в клиенте
Telegram): по строке-запросу возвращает публичные сущности — каналы, группы,
боты, пользователи — с типом, id, заголовком, @username и числом участников.

Исполняется реальным аккаунтом (session_str), как и все остальные Telethon-
операции: ``_make_client`` расшифровывает session_str внутри, connect/disconnect
владеет эта функция. Ошибки не глотаются молча — возвращаются в dict.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0


def _classify(entity) -> str:
    """Тип сущности для UI: channel | group | bot | user."""
    # Telethon Channel: .broadcast=True → канал, .megagroup=True → супергруппа
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or entity.__class__.__name__ in ("Chat", "ChatForbidden"):
        return "group"
    if getattr(entity, "bot", False):
        return "bot"
    return "user"


def _title(entity) -> str:
    if getattr(entity, "title", None):
        return entity.title
    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    name = f"{first} {last}".strip()
    return name or (getattr(entity, "username", "") or "")


async def search_public(
    session_string: str,
    query: str,
    limit: int = 20,
    _acc: dict | None = None,
) -> dict:
    """Глобальный поиск публичных сущностей по строке *query*.

    Возвращает {ok, results: [{type, id, title, username, participants, verified}], error?}.
    ``limit`` ограничивает и запрос к Telegram, и размер ответа (макс. 50).
    """
    query = (query or "").strip().lstrip("@")
    if not query:
        return {"ok": False, "error": "пустой запрос", "results": []}
    limit = max(1, min(int(limit or 20), 50))

    from services.account_manager import _make_client
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.errors import FloodWaitError

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        try:
            found = await asyncio.wait_for(
                client(SearchRequest(q=query, limit=limit)), timeout=25.0
            )
        except FloodWaitError as e:
            return {"ok": False, "error": f"FloodWait {e.seconds}s", "flood_wait": e.seconds, "results": []}

        # contacts.Found: .chats (каналы/группы) + .users (люди/боты).
        # .results/.my_results — Peer'ы (порядок релевантности); сохраняем порядок.
        results: list[dict] = []
        seen: set[tuple[str, int]] = set()

        def _add(entity):
            etype = _classify(entity)
            key = (etype, int(entity.id))
            if key in seen:
                return
            seen.add(key)
            results.append({
                "type": etype,
                "id": int(entity.id),
                "title": _title(entity),
                "username": getattr(entity, "username", None) or None,
                "participants": getattr(entity, "participants_count", None),
                "verified": bool(getattr(entity, "verified", False)),
                "scam": bool(getattr(entity, "scam", False)),
            })

        for ch in getattr(found, "chats", []) or []:
            _add(ch)
        for us in getattr(found, "users", []) or []:
            _add(us)

        return {"ok": True, "results": results[:limit], "query": query}
    except asyncio.TimeoutError:
        return {"ok": False, "error": "таймаут подключения/поиска", "results": []}
    except Exception as e:  # noqa: BLE001 — единичная операция, ошибку отдаём наверх
        log.warning("global_search failed q=%r: %s", query, e)
        return {"ok": False, "error": str(e)[:160], "results": []}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
