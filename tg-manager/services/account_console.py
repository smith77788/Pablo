"""Живая консоль аккаунта: диалоги, история чата, отправка, контакты.

Это РУЧНОЕ управление СВОИМ аккаунтом через его сессию (Telethon) — как обычный
клиент Telegram: посмотреть диалоги, открыть чат, прочитать историю, ответить,
посмотреть контакты. Это не массовая операция (оператор действует руками, 1:1 в
уже существующем диалоге), но всё же реальные сетевые действия под аккаунтом,
поэтому:
  • подключаемся строго через назначенный аккаунту прокси (`_make_client`);
  • на каждый вызов — свежий клиент со своим таймаутом, всегда disconnect;
  • ошибки классифицируем по-русски (протухшая сессия / флуд / нет прав), а не
    отдаём сырой трейс наружу;
  • секреты (session_str) не логируем.

Telethon импортируется ЛЕНИВО внутри функций: в тестовой среде его нет, а чистые
хелперы (`dialog_kind`, `preview`, `classify_error`, `media_label`) должны
тестироваться без сети и без telethon.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 20.0
_ACTION_TIMEOUT = 25.0
# Потолок сообщения из UI — Telegram всё равно режет на 4096; отсекаем заранее.
_MAX_TEXT = 4096


# ── Чистые хелперы (тестируются без сети) ────────────────────────────────────

def dialog_kind(is_user: bool, is_bot: bool, is_group: bool, is_channel: bool) -> str:
    """Тип диалога одним словом для UI. Порядок важен: бот — это тоже user, но
    показать его отдельно полезнее, поэтому бот проверяется раньше user."""
    if is_bot:
        return "bot"
    if is_user:
        return "user"
    if is_group:
        return "group"
    if is_channel:
        return "channel"
    return "unknown"


def preview(text: str | None, limit: int = 80) -> str:
    """Однострочный превью последнего сообщения: без переносов, обрезка с «…»."""
    if not text:
        return ""
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def media_label(media_type: str | None) -> str:
    """Иконка+подпись для нетекстового сообщения. media_type — имя типа Telethon."""
    if not media_type:
        return ""
    m = media_type.lower()
    if "photo" in m:
        return "📷 Фото"
    if "webpage" in m:
        return "🔗 Ссылка"
    if "poll" in m:
        return "📊 Опрос"
    if "geo" in m or "venue" in m:
        return "📍 Геолокация"
    if "contact" in m:
        return "👤 Контакт"
    if "document" in m or "media" in m:
        return "📎 Файл"
    return "📎 Вложение"


def classify_error(msg: str) -> tuple[str, str]:
    """Сырое сообщение ошибки → (код, человекочитаемая причина по-русски).

    Код — машинный (для фронта/логики), текст — для пользователя. Держим здесь,
    а не в endpoint, чтобы бот и мини-апп классифицировали одинаково.
    """
    s = (msg or "").lower()
    if any(k in s for k in ("auth", "unauthorized", "unregistered", "session",
                            "authkey", "key is invalid")):
        return ("session_expired", "🔑 Сессия недействительна — нужна переавторизация.")
    if "flood" in s:
        return ("flood", "⏳ Слишком часто (FloodWait) — дайте аккаунту отдохнуть.")
    if any(k in s for k in ("privacy", "not mutual")):
        return ("privacy", "🔒 Настройки приватности собеседника не позволяют написать.")
    if any(k in s for k in ("write forbidden", "chat_write", "banned in", "channelprivate")):
        return ("no_access", "🚫 Нет прав писать в этот чат.")
    if any(k in s for k in ("proxy", "connect", "timeout", "network", "socks")):
        return ("network", "🌐 Проблема с прокси или сетью — проверьте прокси аккаунта.")
    if "could not find" in s or "cannot find" in s or "no user has" in s:
        return ("not_found", "❓ Собеседник не найден — начните диалог из общего чата.")
    return ("error", f"⚠️ Ошибка: {(msg or 'неизвестная')[:120]}")


def parse_peer(raw: str) -> int | str:
    """Идентификатор собеседника из URL: числовой (в т.ч. отрицательный —
    «marked» id канала/чата) → int, иначе оставляем строкой (@username)."""
    raw = (raw or "").strip()
    if re.match(r"^-?\d+$", raw):
        return int(raw)
    return raw


# ── Работа с сессией ─────────────────────────────────────────────────────────

async def _warm_entity(client: Any, peer: int | str) -> Any:
    """Разрешить собеседника в InputPeer.

    `StringSession` НЕ хранит кэш сущностей между подключениями, поэтому
    `get_input_entity(id)` на свежем клиенте часто падает с «Could not find the
    input entity». Лечение — один раз прогреть кэш через `get_dialogs`, тогда id
    из нашего же списка диалогов разрешается. Для @username прогрев не нужен.
    """
    try:
        return await client.get_input_entity(peer)
    except (ValueError, TypeError):
        await asyncio.wait_for(client.get_dialogs(limit=200), timeout=_ACTION_TIMEOUT)
        return await client.get_input_entity(peer)


async def list_dialogs(session_string: str, acc: dict | None,
                       limit: int = 40) -> dict[str, Any]:
    """Список диалогов аккаунта (последние `limit`). Только чтение."""
    from services.account_manager import _make_client

    client = _make_client(session_string, acc)
    out: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        dialogs = await asyncio.wait_for(client.get_dialogs(limit=limit),
                                         timeout=_ACTION_TIMEOUT)
        for dg in dialogs:
            ent = dg.entity
            last = getattr(dg, "message", None)
            last_media = None
            if last is not None and getattr(last, "media", None) is not None:
                last_media = type(last.media).__name__
            out.append({
                "id": dg.id,
                "name": dg.name or "Без названия",
                "kind": dialog_kind(bool(getattr(dg, "is_user", False)),
                                    bool(getattr(ent, "bot", False)),
                                    bool(getattr(dg, "is_group", False)),
                                    bool(getattr(dg, "is_channel", False))),
                "username": getattr(ent, "username", None),
                "unread": int(getattr(dg, "unread_count", 0) or 0),
                "pinned": bool(getattr(dg, "pinned", False)),
                "last_text": preview(getattr(last, "message", None)),
                "last_media": media_label(last_media),
                "last_out": bool(getattr(last, "out", False)) if last else False,
                "date": dg.date.isoformat() if getattr(dg, "date", None) else None,
            })
        return {"ok": True, "dialogs": out}
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.list_dialogs acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_history(session_string: str, acc: dict | None, peer: int | str,
                      limit: int = 40) -> dict[str, Any]:
    """История одного диалога (последние `limit` сообщений, новые внизу)."""
    from services.account_manager import _make_client

    client = _make_client(session_string, acc)
    msgs: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer), timeout=_ACTION_TIMEOUT)
        peer_name = None
        try:
            ent_obj = await client.get_entity(entity)
            peer_name = getattr(ent_obj, "title", None) or " ".join(
                x for x in (getattr(ent_obj, "first_name", None),
                            getattr(ent_obj, "last_name", None)) if x) or None
        except Exception:
            pass
        collected = await asyncio.wait_for(
            client.get_messages(entity, limit=limit), timeout=_ACTION_TIMEOUT)
        for m in collected:
            media = type(m.media).__name__ if getattr(m, "media", None) is not None else None
            msgs.append({
                "id": m.id,
                "out": bool(getattr(m, "out", False)),
                "text": m.message or "",
                "media": media_label(media),
                "date": m.date.isoformat() if getattr(m, "date", None) else None,
                "sender_id": getattr(m, "sender_id", None),
            })
        msgs.reverse()  # get_messages отдаёт от новых к старым — в UI новые внизу
        return {"ok": True, "peer_name": peer_name, "messages": msgs}
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.get_history acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_text(session_string: str, acc: dict | None, peer: int | str,
                    text: str) -> dict[str, Any]:
    """Отправить текстовое сообщение в диалог. Ручное действие 1:1.

    FloodWait/приватность/нет прав возвращаются классифицированной ошибкой, а не
    трейсом — оператор должен понять, почему не ушло.
    """
    from services.account_manager import _make_client

    text = (text or "").strip()[:_MAX_TEXT]
    if not text:
        return {"ok": False, "code": "empty", "error": "Пустое сообщение"}

    client = _make_client(session_string, acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer), timeout=_ACTION_TIMEOUT)
        sent = await asyncio.wait_for(client.send_message(entity, text),
                                      timeout=_ACTION_TIMEOUT)
        return {"ok": True, "message_id": getattr(sent, "id", None)}
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.send_text acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def list_contacts(session_string: str, acc: dict | None) -> dict[str, Any]:
    """Контакты аккаунта (адресная книга Telegram). Только чтение."""
    from services.account_manager import _make_client

    client = _make_client(session_string, acc)
    out: list[dict] = []
    try:
        from telethon.tl.functions.contacts import GetContactsRequest
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        res = await asyncio.wait_for(client(GetContactsRequest(hash=0)),
                                     timeout=_ACTION_TIMEOUT)
        users = getattr(res, "users", []) or []
        for u in users:
            name = " ".join(x for x in (getattr(u, "first_name", None),
                                        getattr(u, "last_name", None)) if x)
            out.append({
                "id": u.id,
                "name": name or (("@" + u.username) if getattr(u, "username", None) else str(u.id)),
                "username": getattr(u, "username", None),
                "phone": getattr(u, "phone", None),
                "is_bot": bool(getattr(u, "bot", False)),
                "premium": bool(getattr(u, "premium", False)),
            })
        out.sort(key=lambda c: c["name"].lower())
        return {"ok": True, "contacts": out}
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.list_contacts acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
