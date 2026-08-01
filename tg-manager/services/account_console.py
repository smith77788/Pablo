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

async def _warm_entity(client: Any, peer: int | str,
                       access_hash: int | None = None) -> Any:
    """Разрешить собеседника в InputPeer.

    `StringSession` НЕ хранит кэш сущностей между подключениями, поэтому
    `get_input_entity(id)` на свежем клиенте часто падает с «Could not find the
    input entity». Три пути разрешения, от дешёвого к дорогому:

    1. Есть `access_hash` (мы знаем его для контакта) — строим `InputPeerUser`
       напрямую, без единого запроса. Это и даёт «начать диалог с ЛЮБЫМ
       контактом», даже без @username и без общей переписки.
    2. Прогрев `get_dialogs` — id из нашего списка диалогов разрешается.
    3. Прогрев адресной книги (`GetContactsRequest`) — контакт вне последних
       диалогов. Для @username всё это не нужно — резолвится сразу.
    """
    if access_hash is not None and isinstance(peer, int) and peer > 0:
        from telethon.tl.types import InputPeerUser
        return InputPeerUser(peer, int(access_hash))
    try:
        return await client.get_input_entity(peer)
    except (ValueError, TypeError):
        await asyncio.wait_for(client.get_dialogs(limit=200), timeout=_ACTION_TIMEOUT)
        try:
            return await client.get_input_entity(peer)
        except (ValueError, TypeError):
            from telethon.tl.functions.contacts import GetContactsRequest
            await asyncio.wait_for(client(GetContactsRequest(hash=0)),
                                   timeout=_ACTION_TIMEOUT)
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
                # access_hash — 64-битное число: отдаём СТРОКОЙ, иначе JS потеряет
                # точность (>2^53) и резолв по InputPeerUser сломается.
                "access_hash": (lambda h: str(h) if h is not None else None)(
                    getattr(ent, "access_hash", None)),
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
                      limit: int = 40, access_hash: int | None = None) -> dict[str, Any]:
    """История одного диалога (последние `limit` сообщений, новые внизу)."""
    from services.account_manager import _make_client

    client = _make_client(session_string, acc)
    msgs: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer, access_hash),
                                        timeout=_ACTION_TIMEOUT)
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
                    text: str, access_hash: int | None = None) -> dict[str, Any]:
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
        entity = await asyncio.wait_for(_warm_entity(client, peer, access_hash),
                                        timeout=_ACTION_TIMEOUT)
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


async def send_file(session_string: str, acc: dict | None, peer: int | str,
                    file_bytes: bytes, filename: str, caption: str = "",
                    access_hash: int | None = None) -> dict[str, Any]:
    """Отправить файл (фото/документ) в диалог. Ручное действие 1:1.

    Telethon сам определит изображение и отправит как фото, остальное — как
    документ. Загрузка дольше текста → отдельный, больший таймаут.
    """
    import io
    from services.account_manager import _make_client

    if not file_bytes:
        return {"ok": False, "code": "empty", "error": "Пустой файл"}

    client = _make_client(session_string, acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer, access_hash),
                                        timeout=_ACTION_TIMEOUT)
        bio = io.BytesIO(file_bytes)
        bio.name = filename or "file"
        sent = await asyncio.wait_for(
            client.send_file(entity, bio, caption=(caption or "")[:1024] or None),
            timeout=120)
        return {"ok": True, "message_id": getattr(sent, "id", None)}
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.send_file acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _to_ui_contact(c: dict) -> dict:
    """Богатый dict из account_manager → компактный формат для консоли."""
    name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
    uname = c.get("username") or None
    return {
        "id": c.get("user_id"),
        "name": name or (("@" + uname) if uname else str(c.get("user_id"))),
        "username": uname,
        "phone": c.get("phone") or None,
        # access_hash строкой (64-бит > 2^53 → JS теряет точность). Позволяет
        # написать контакту без общего чата.
        "access_hash": (str(c["access_hash"]) if c.get("access_hash") is not None else None),
        "is_bot": False,  # оба источника исключают ботов и удалённых
        "premium": bool(c.get("is_premium")),
        "mutual": bool(c.get("is_mutual")),
    }


async def list_contacts(session_string: str, acc: dict | None) -> dict[str, Any]:
    """Контакты аккаунта. Только чтение.

    Два источника, как в синхронизации контакт-хаба: адресная книга
    (`get_contacts`) И собеседники личных диалогов (`get_dialog_contacts`). У
    «рабочего» аккаунта книга часто пуста, а в ЛС — десятки людей; без второго
    источника карточка показывала бы «нет контактов» при живой переписке (эту же
    ошибку уже ловили в контакт-хабе). Переиспользуем движок account_manager,
    а не дублируем GetContactsRequest здесь.
    """
    from services import account_manager as am

    try:
        book = await am.get_contacts(session_string, acc)
    except Exception as exc:
        code, human = classify_error(str(exc))
        log.warning("account_console.list_contacts acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}

    # Диалоговые собеседники — дополнение. Их сбой НЕ роняет уже полученную книгу.
    dialog_people: list[dict] = []
    try:
        dialog_people = await am.get_dialog_contacts(session_string, limit=300, _acc=acc)
    except Exception:
        log.debug("list_contacts: dialog harvest failed acc=%s", (acc or {}).get("id"))

    # Слияние по user_id: адресная книга приоритетнее (там телефон/mutual), диалоги
    # лишь ДОБАВЛЯЮТ недостающих — поэтому книга идёт первой и не перезатирается.
    by_id: dict[Any, dict] = {}
    for c in list(book) + dialog_people:
        uid = c.get("user_id")
        if uid is None or uid in by_id:
            continue
        by_id[uid] = c
    out = [_to_ui_contact(c) for c in by_id.values()]
    out.sort(key=lambda c: c["name"].lower())
    return {"ok": True, "contacts": out}
