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
import random
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


def human_wait(seconds: int) -> str:
    """Длительность паузы словами владельца. «3600 с» ему ничего не говорит."""
    secs = max(0, int(seconds or 0))
    if secs < 60:
        return f"{secs} с"
    if secs < 3600:
        return f"{secs // 60} мин"
    hours, rest = divmod(secs, 3600)
    minutes = rest // 60
    return f"{hours} ч" if not minutes else f"{hours} ч {minutes} мин"


def classify_error(msg: str) -> tuple[str, str]:
    """Сырое сообщение ошибки → (код, человекочитаемая причина по-русски).

    Код — машинный (для фронта/логики), текст — для пользователя. Держим здесь,
    а не в endpoint, чтобы бот и мини-апп классифицировали одинаково.
    """
    s = (msg or "").lower()
    if any(k in s for k in ("auth", "unauthorized", "unregistered", "session",
                            "authkey", "key is invalid")):
        return ("session_expired", "🔑 Сессия недействительна — нужна переавторизация.")
    # Сколько ждать — у Telegram это число есть всегда, и владельцу оно нужнее
    # слова «часто»: по нему видно, отдохнуть минуту или отложить на час.
    # Формы: наш FloodHandoff («FloodWait handoff: 45s») и сырой текст telethon
    # («A wait of 45 seconds is required…»), который до сюда доходит из кода,
    # не обёрнутого предохранителем — и без этой ветки уезжал владельцу
    # по-английски.
    _wait = re.search(r"wait[^0-9]{0,20}(\d+)\s*(?:s\b|sec|second)", s)
    if _wait:
        secs = int(_wait.group(1))
        # Опознаём слоу-мод по ТЕКСТУ telethon: слова «slow» в нём нет, есть
        # только «before sending another message in this chat» (сверено с
        # telethon 1.36.0, SlowModeWaitError). Проверка на «slow» пропускала
        # самое частое ожидание при отправке в чат.
        if "another message in this chat" in s or "slowmode" in s or "slow mode" in s:
            return ("slow_mode",
                    f"🐢 В чате включён медленный режим — следующее сообщение "
                    f"можно отправить через {human_wait(secs)}.")
        return ("flood", f"⏳ Telegram просит паузу {human_wait(secs)} — "
                         f"дайте аккаунту отдохнуть.")
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


def typing_duration(text_len: int) -> float:
    """Правдоподобная длительность «печатает…» по длине текста.

    Гаусс вокруг оценки набора (≈220 знаков/мин) + clamp [0.6; 8.0] c — чтобы не
    было ни мгновенной отправки, ни аномально долгой паузы (оба — палевные тайминги).
    Чистая функция — тестируется без сети.
    """
    n = max(0, int(text_len or 0))
    base = min(0.6 + n * (60.0 / 220.0) / 60.0 * 3.0, 6.0)  # растёт с длиной, потолок ~6с
    sampled = random.gauss(base, base * 0.25)
    return max(0.6, min(sampled, 8.0))


async def _simulate_typing(client: Any, entity: Any, text: str) -> None:
    """Показать статус «печатает…» и выдержать паузу набора. Fail-open: любая
    ошибка (нет прав/сеть) не мешает самой отправке."""
    try:
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction
        await asyncio.wait_for(
            client(SetTypingRequest(entity, SendMessageTypingAction())),
            timeout=_ACTION_TIMEOUT)
    except Exception:
        return  # статус не критичен — молча продолжаем к отправке
    await asyncio.sleep(typing_duration(len(text or "")))


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


async def _claim(acc: dict | None) -> "tuple[int | None, bool]":
    """Захватить аккаунт консоли под живую сессию. Возвращает (acc_id, занят?).

    Консоль — такая же живая сессия, как операция: пользователь листает диалоги,
    а аккаунт в этот момент может вести массовую операцию или прогрев. Без
    захвата на одном auth-key оказывались две сессии → AUTH_KEY_DUPLICATED.

    Захват делается ЗДЕСЬ, а не в account_manager: он не реентрантный, и если
    захватывать в общем библиотечном слое, повторный захват изнутри уже
    захваченной операции получил бы отказ. Правило: захватывает точка входа.

    acc без id (служебные вызовы) — работаем как раньше, без захвата.
    """
    acc_id = int((acc or {}).get("id") or 0)
    if not acc_id:
        return None, False
    from services import op_worker as _opw
    if not await _opw.try_claim_account(acc_id):
        return None, True
    return acc_id, False


async def _unclaim(acc_id: "int | None") -> None:
    if not acc_id:
        return
    from services import op_worker as _opw
    await _opw.release_accounts([int(acc_id)])


async def note_error(pool, acc: dict | None, exc: BaseException) -> tuple[str, str]:
    """Ошибка живой консоли → (код, текст по-русски) + запись паузы Telegram.

    Консоль ходит в Telegram тем же аккаунтом, что и массовые операции. Если
    Telegram попросил паузу, а мы только показали это владельцу и забыли —
    для остального продукта аккаунт остаётся «спокойным»: выбор аккаунта под
    следующую операцию возьмёт его снова и уведёт под действующее ограничение,
    где следующая пауза будет длиннее предыдущей.

    Медленный режим чата (`slow_mode`) сюда НЕ попадает: это свойство чата, а
    не аккаунта, и ставить из-за него аккаунт на кулдаун — значит без причины
    вывести здоровый аккаунт из работы.
    """
    code, human = classify_error(str(exc))
    acc_id = int((acc or {}).get("id") or 0)
    if code == "flood" and acc_id:
        try:
            from services import flood_engine as _fe

            secs = _fe.flood_seconds(exc) or 0
            if secs > 0:
                await _fe.record_flood(pool, acc_id, int(secs), "console")
        except Exception:
            log.warning("account_console: пауза Telegram не записана в пульс здоровья",
                        exc_info=True)
    return code, human


_BUSY_RESULT = {
    "ok": False,
    "code": "busy",
    "error": "⏳ Аккаунт занят другой операцией — попробуйте через минуту.",
}


async def list_dialogs(session_string: str, acc: dict | None,
                       limit: int = 40, pool=None) -> dict[str, Any]:
    """Список диалогов аккаунта (последние `limit`). Только чтение."""
    from services.account_manager import _make_client

    _acc_id, _busy = await _claim(acc)
    if _busy:
        return dict(_BUSY_RESULT)
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
        code, human = await note_error(pool, acc, exc)
        log.warning("account_console.list_dialogs acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await _unclaim(_acc_id)


async def get_history(session_string: str, acc: dict | None, peer: int | str,
                      limit: int = 40, access_hash: int | None = None,
                      pool=None) -> dict[str, Any]:
    """История одного диалога (последние `limit` сообщений, новые внизу)."""
    from services.account_manager import _make_client

    _acc_id, _busy = await _claim(acc)
    if _busy:
        return dict(_BUSY_RESULT)
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
        from services.telethon_guard import guarded_call
        collected = await guarded_call(
            client,
            lambda: asyncio.wait_for(client.get_messages(entity, limit=limit),
                                     timeout=_ACTION_TIMEOUT),
            action="history")
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
        code, human = await note_error(pool, acc, exc)
        log.warning("account_console.get_history acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await _unclaim(_acc_id)


async def send_text(session_string: str, acc: dict | None, peer: int | str,
                    text: str, access_hash: int | None = None,
                    pool=None) -> dict[str, Any]:
    """Отправить текстовое сообщение в диалог. Ручное действие 1:1.

    FloodWait/приватность/нет прав возвращаются классифицированной ошибкой, а не
    трейсом — оператор должен понять, почему не ушло.
    """
    from services.account_manager import _make_client

    text = (text or "").strip()[:_MAX_TEXT]
    if not text:
        return {"ok": False, "code": "empty", "error": "Пустое сообщение"}

    _acc_id, _busy = await _claim(acc)
    if _busy:
        return dict(_BUSY_RESULT)
    client = _make_client(session_string, acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer, access_hash),
                                        timeout=_ACTION_TIMEOUT)
        # Естественное поведение клиента: перед отправкой в ЛС показать «печатает…»
        # (корректно информирует API о действии). Длительность — по длине текста с
        # гауссовым джиттером и clamp, чтобы не было аномально ровных таймингов.
        await _simulate_typing(client, entity, text)
        from services.telethon_guard import guarded_call
        sent = await guarded_call(
            client,
            lambda: asyncio.wait_for(client.send_message(entity, text),
                                     timeout=_ACTION_TIMEOUT),
            action="dm_send")
        return {"ok": True, "message_id": getattr(sent, "id", None)}
    except Exception as exc:
        code, human = await note_error(pool, acc, exc)
        log.warning("account_console.send_text acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await _unclaim(_acc_id)


async def send_file(session_string: str, acc: dict | None, peer: int | str,
                    file_bytes: bytes, filename: str, caption: str = "",
                    access_hash: int | None = None,
                    pool=None) -> dict[str, Any]:
    """Отправить файл (фото/документ) в диалог. Ручное действие 1:1.

    Telethon сам определит изображение и отправит как фото, остальное — как
    документ. Загрузка дольше текста → отдельный, больший таймаут.
    """
    import io
    from services.account_manager import _make_client

    if not file_bytes:
        return {"ok": False, "code": "empty", "error": "Пустой файл"}

    _acc_id, _busy = await _claim(acc)
    if _busy:
        return dict(_BUSY_RESULT)
    client = _make_client(session_string, acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await asyncio.wait_for(_warm_entity(client, peer, access_hash),
                                        timeout=_ACTION_TIMEOUT)
        bio = io.BytesIO(file_bytes)
        bio.name = filename or "file"
        from services.telethon_guard import guarded_call
        sent = await guarded_call(
            client,
            lambda: asyncio.wait_for(
                client.send_file(entity, bio, caption=(caption or "")[:1024] or None),
                timeout=120),
            action="dm_file")
        return {"ok": True, "message_id": getattr(sent, "id", None)}
    except Exception as exc:
        code, human = await note_error(pool, acc, exc)
        log.warning("account_console.send_file acc=%s: %s", (acc or {}).get("id"), exc)
        return {"ok": False, "code": code, "error": human}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await _unclaim(_acc_id)


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


async def list_contacts(session_string: str, acc: dict | None,
                        pool=None) -> dict[str, Any]:
    """Контакты аккаунта. Только чтение.

    Два источника, как в синхронизации контакт-хаба: адресная книга
    (`get_contacts`) И собеседники личных диалогов (`get_dialog_contacts`). У
    «рабочего» аккаунта книга часто пуста, а в ЛС — десятки людей; без второго
    источника карточка показывала бы «нет контактов» при живой переписке (эту же
    ошибку уже ловили в контакт-хабе). Переиспользуем движок account_manager,
    а не дублируем GetContactsRequest здесь.
    """
    from services import account_manager as am

    # Сессию открывает account_manager, но точка входа — здесь, поэтому и захват
    # здесь (см. докстринг _claim: захватывает вход, не библиотечный слой).
    _acc_id, _busy = await _claim(acc)
    if _busy:
        return dict(_BUSY_RESULT)
    try:
        try:
            book = await am.get_contacts(session_string, acc)
        except Exception as exc:
            code, human = await note_error(pool, acc, exc)
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
    finally:
        await _unclaim(_acc_id)
