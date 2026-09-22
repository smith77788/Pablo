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


async def _note_flood(pool, _acc: dict | None, seconds: int) -> None:
    """Флуд при поиске обязан попасть в общий пульс здоровья аккаунта.

    Без этого для остальных подсистем аккаунт остаётся «спокойным»: выбор
    аккаунта под следующую операцию берёт его снова и уводит под действующее
    ограничение Telegram — а следующий FloodWait будет длиннее предыдущего.
    """
    acc_id = (_acc or {}).get("id")
    if not acc_id:
        return
    try:
        from services import flood_engine as _fe

        await _fe.record_flood(pool, int(acc_id), int(seconds), "search")
    except Exception:
        log.warning("global_search: FloodWait не записан в пульс здоровья",
                    exc_info=True)


def _ru_error(msg: str) -> tuple[str, str]:
    """Сырая ошибка Telegram → (код, текст по-русски).

    Текст отсюда уходит владельцу напрямую: бот показывает его как
    «⚠️ {error}», мини-апп — телом ответа. Раньше сюда уезжало
    «FloodWait 300s» и сырой текст telethon по-английски. Классификацию не
    заводим свою — берём общую из account_console, чтобы бот, консоль и поиск
    объясняли одну и ту же ошибку одинаково.
    """
    try:
        from services.account_console import classify_error

        return classify_error(msg)
    except Exception:
        return ("error", f"⚠️ Ошибка: {(msg or 'неизвестная')[:120]}")


async def search_public(
    session_string: str,
    query: str,
    limit: int = 20,
    _acc: dict | None = None,
    pool=None,
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
            _secs = int(getattr(e, "seconds", 0) or 0)
            await _note_flood(pool, _acc, _secs)
            _code, _text = _ru_error(str(e))
            return {"ok": False, "error": _text, "error_code": _code,
                    "flood_wait": _secs, "results": []}

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
        return {"ok": False, "error": "⌛ Аккаунт не ответил вовремя — проверьте прокси и сессию.",
                "error_code": "timeout", "results": []}
    except Exception as e:  # noqa: BLE001 — единичная операция, ошибку отдаём наверх
        # Сюда падает и FloodWait с фазы connect: он поднимается ДО внутреннего
        # try, и раньше уезжал владельцу сырым английским текстом, а в пульс
        # здоровья не попадал вовсе.
        log.warning("global_search failed q=%r: %s", query, e)
        _code, _text = _ru_error(str(e))
        _out = {"ok": False, "error": _text, "error_code": _code, "results": []}
        if _code == "flood":
            from services import parser as _parser

            _secs = _parser.flood_seconds(e) or 0
            if _secs:
                await _note_flood(pool, _acc, _secs)
                _out["flood_wait"] = _secs
        return _out
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
