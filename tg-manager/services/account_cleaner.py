"""
Account Cleaner — очистка и восстановление аккаунтов.

Операции:
- leave_all_chats: выйти из всех групп/каналов (кроме whitelist)
- cleanup_dialogs: очистить историю личных сообщений
- delete_contacts: удалить все контакты
- cleanup_old_messages: удалить старые сообщения аккаунта в группах

Используется для:
- сброса аккаунта перед новым назначением
- очистки следов активности
- подготовки к transfer/архивированию
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Callable, Any


from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


# ── Отбор целей очистки ──────────────────────────────────────────────────
# Один отбор на бот и на исполнитель операций. Раньше исполнитель очереди
# (мини-апп «Очистка» и массовое «выйти из всех» в списке аккаунтов) выходил
# из ВСЕХ групп и каналов подряд, включая каналы, которые этот аккаунт ведёт
# в системе: белый список управляемых каналов был только у бота. А удаление
# личных диалогов сносило «Избранное» (диалог с самим собой) и чат Telegram
# с кодами входа (777000).

CLEANER_KINDS = ("all", "channels", "groups")
CONTACT_MODES = ("all", "non_mutual")
# Служебный чат Telegram: сюда приходят коды входа. Удалять его переписку
# значит терять историю кодов и подтверждений — не трогаем никогда.
TELEGRAM_SERVICE_ID = 777000


def norm_chat_ref(raw) -> str:
    """Ссылка на чат в виде, в котором её отдаёт сущность Telethon.

    Принимает id (в том числе полный -100xxxxxxxxxx), @username и ссылку
    t.me/username. Числа приводятся к короткому положительному виду, имена —
    к нижнему регистру без «@». Пустое — пустая строка.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    for pref in ("https://", "http://"):
        if s.lower().startswith(pref):
            s = s[len(pref):]
    if s.lower().startswith("t.me/"):
        s = s[5:]
    s = s.split("?", 1)[0].strip("/").lstrip("@").strip()
    if s.lstrip("-").isdigit():
        digits = s.lstrip("-")
        if s.startswith("-100") and len(digits) > 10:
            digits = digits[3:]
        return str(int(digits)) if digits else ""
    return s.lower()


def parse_whitelist(text) -> list[str]:
    """Белый список из свободного текста: через запятую, пробел или строку."""
    import re as _re

    if isinstance(text, (list, tuple)):
        parts = [str(x) for x in text]
    else:
        parts = _re.split(r"[\s,;]+", str(text or ""))
    out: list[str] = []
    for p in parts:
        n = norm_chat_ref(p)
        if n and n not in out:
            out.append(n)
    return out[:500]


def _dialog_age_days(dialog, now) -> float | None:
    d = getattr(dialog, "date", None)
    if d is None:
        return None
    try:
        if d.tzinfo is None:
            from datetime import timezone as _tz
            d = d.replace(tzinfo=_tz.utc)
        return (now - d).total_seconds() / 86400.0
    except Exception:
        return None


def pick_chats_to_leave(
    dialogs,
    *,
    kinds: str = "all",
    keep_own: bool = True,
    own_ids=(),
    whitelist=(),
    inactive_days: int = 0,
    now=None,
) -> tuple[list, dict]:
    """Какие группы и каналы покинуть.

    Возвращает (диалоги к выходу, счётчики пропусков). Пропускается:
    * чужой вид (`kinds`: только каналы или только группы);
    * свои — каналы из каталога системы (`own_ids`) и чаты, где аккаунт
      создатель или админ, если `keep_own`;
    * белый список (id или @username);
    * чаты с сообщениями свежее `inactive_days` дней, если задано.
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    kinds = kinds if kinds in CLEANER_KINDS else "all"
    own = {norm_chat_ref(x) for x in (own_ids or ())} - {""}
    white = {norm_chat_ref(x) for x in (whitelist or ())} - {""}
    skipped = {"kind": 0, "own": 0, "whitelist": 0, "active": 0}
    picked = []
    for d in dialogs:
        is_group = bool(getattr(d, "is_group", False))
        is_channel = bool(getattr(d, "is_channel", False))
        if not (is_group or is_channel):
            continue
        if kinds == "channels" and is_group:
            skipped["kind"] += 1
            continue
        if kinds == "groups" and not is_group:
            skipped["kind"] += 1
            continue
        ent = getattr(d, "entity", None)
        eid = norm_chat_ref(getattr(ent, "id", ""))
        uname = norm_chat_ref(getattr(ent, "username", "") or "")
        if keep_own and (
            eid in own
            or bool(getattr(ent, "creator", False))
            or getattr(ent, "admin_rights", None) is not None
        ):
            skipped["own"] += 1
            continue
        if eid in white or (uname and uname in white):
            skipped["whitelist"] += 1
            continue
        if inactive_days and inactive_days > 0:
            age = _dialog_age_days(d, now)
            if age is not None and age < inactive_days:
                skipped["active"] += 1
                continue
        picked.append(d)
    return picked, skipped


def pick_private_dialogs(dialogs, *, inactive_days: int = 0, keep_bots: bool = False, now=None):
    """Какие личные диалоги удалить. «Избранное» (сам себе) и служебный чат
    Telegram с кодами входа не удаляются никогда."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    skipped = {"protected": 0, "active": 0, "bots": 0}
    picked = []
    for d in dialogs:
        if not getattr(d, "is_user", False):
            continue
        ent = getattr(d, "entity", None)
        if getattr(ent, "is_self", False) or getattr(ent, "id", None) == TELEGRAM_SERVICE_ID:
            skipped["protected"] += 1
            continue
        if keep_bots and getattr(ent, "bot", False):
            skipped["bots"] += 1
            continue
        if inactive_days and inactive_days > 0:
            age = _dialog_age_days(d, now)
            if age is not None and age < inactive_days:
                skipped["active"] += 1
                continue
        picked.append(d)
    return picked, skipped


def pick_contacts(users, mode: str = "all") -> list:
    """Какие контакты удалить: все или только невзаимные (вас нет у них)."""
    if mode == "non_mutual":
        return [u for u in users if not getattr(u, "mutual_contact", False)]
    return list(users)


def skipped_note(skipped: dict) -> str:
    """Человеческая строка о пропущенном: «оставлено: свои 3, белый список 1»."""
    names = {"own": "свои", "whitelist": "белый список", "active": "активные",
             "kind": "другой вид", "protected": "Избранное и служебные",
             "bots": "боты"}
    parts = [f"{names.get(k, k)} {v}" for k, v in skipped.items() if v]
    return ("оставлено: " + ", ".join(parts)) if parts else ""


async def leave_all_chats(
    session_string: str,
    acc: dict | None = None,
    whitelist: list[int | str] | None = None,
    dry_run: bool = False,
    progress_cb: Optional[Callable[[int, str], Any]] = None,
) -> dict:
    """
    Выйти из всех групп и каналов аккаунта.
    whitelist: список ID или username которые НЕ трогать.
    dry_run: только подсчёт, без действий.
    Возвращает {'left': int, 'skipped': int, 'errors': list}
    """
    from services import account_manager

    client = account_manager._make_client(session_string, acc)
    # Потолок на КАЖДЫЙ запрос. Эти три функции зовут обработчики бота, а
    # у aiogram таймаута шлюза нет: мёртвый прокси открывает соединение и
    # замолкает, и владелец, нажавший кнопку, не получает НИ ответа, ни
    # ошибки — навсегда. См. account_manager._Timeboxed.
    client = account_manager.timeboxed(client)
    left = 0
    skipped = 0
    errors = []
    whitelist_set = set(str(x) for x in (whitelist or []))

    try:
        await asyncio.wait_for(client.connect(), timeout=15)

        dialogs = await client.get_dialogs(limit=500)
        # Отбор общий с исполнителем операций: свои (создатель/админ) и
        # белый список не трогаются.
        chats, skip_counts = pick_chats_to_leave(dialogs, whitelist=list(whitelist_set))
        skipped = sum(skip_counts.values())

        for i, dialog in enumerate(chats):
            entity = dialog.entity
            entity_id = str(getattr(entity, "id", ""))

            if progress_cb:
                try:
                    await progress_cb(i + 1, dialog.name or entity_id)
                except Exception:
                    log_exc_swallow(log, "Сбой progress_cb в leave_all_chats")

            if not dry_run:
                try:
                    from telethon.tl.types import Chat as TLChat

                    if dialog.is_channel:
                        # Supergroup or broadcast channel
                        from telethon.tl.functions.channels import LeaveChannelRequest
                        await client(LeaveChannelRequest(entity))
                    elif dialog.is_group and isinstance(entity, TLChat):
                        # Legacy basic group — uses DeleteChatUserRequest
                        from telethon.tl.functions.messages import DeleteChatUserRequest
                        me = await client.get_me()
                        await client(DeleteChatUserRequest(entity.id, me))
                    else:
                        # Megagroup / other channel-type group
                        from telethon.tl.functions.channels import LeaveChannelRequest
                        await client(LeaveChannelRequest(entity))
                    left += 1
                    await asyncio.sleep(1.5)
                except Exception as e:
                    errors.append(f"{dialog.name}: {str(e)[:80]}")
            else:
                left += 1  # dry_run: считаем

    except Exception as e:
        log.warning("leave_all_chats error: %s", e)
        errors.append(str(e)[:200])
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в leave_all_chats")

    return {"left": left, "skipped": skipped, "errors": errors[:10], "dry_run": dry_run}


async def delete_contacts(
    session_string: str,
    acc: dict | None = None,
) -> dict:
    """Удалить все контакты аккаунта."""
    from services import account_manager

    client = account_manager._make_client(session_string, acc)
    # Потолок на КАЖДЫЙ запрос. Эти три функции зовут обработчики бота, а
    # у aiogram таймаута шлюза нет: мёртвый прокси открывает соединение и
    # замолкает, и владелец, нажавший кнопку, не получает НИ ответа, ни
    # ошибки — навсегда. См. account_manager._Timeboxed.
    client = account_manager.timeboxed(client)
    deleted = 0

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        from telethon.tl.functions.contacts import (
            GetContactsRequest,
            DeleteContactsRequest,
        )

        result = await client(GetContactsRequest(hash=0))
        contacts = result.users if hasattr(result, "users") else []

        if contacts:
            user_ids = [u.id for u in contacts]
            await client(DeleteContactsRequest(id=user_ids))
            deleted = len(user_ids)

    except Exception as e:
        log.warning("delete_contacts error: %s", e)
        return {"deleted": 0, "error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в delete_contacts")

    return {"deleted": deleted}


async def get_chat_list_for_cleanup(
    session_string: str,
    acc: dict | None = None,
) -> list[dict]:
    """Получить список чатов для выбора очистки."""
    from services import account_manager

    client = account_manager._make_client(session_string, acc)
    # Потолок на КАЖДЫЙ запрос. Эти три функции зовут обработчики бота, а
    # у aiogram таймаута шлюза нет: мёртвый прокси открывает соединение и
    # замолкает, и владелец, нажавший кнопку, не получает НИ ответа, ни
    # ошибки — навсегда. См. account_manager._Timeboxed.
    client = account_manager.timeboxed(client)
    chats = []

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        dialogs = await client.get_dialogs(limit=200)

        for d in dialogs:
            entity = d.entity
            chat_type = "group" if d.is_group else ("channel" if d.is_channel else "pm")
            chats.append(
                {
                    "id": getattr(entity, "id", 0),
                    "title": d.name or str(getattr(entity, "id", 0)),
                    "username": getattr(entity, "username", None),
                    "type": chat_type,
                    "members": getattr(entity, "participants_count", None),
                }
            )
    except Exception as e:
        log.warning("get_chat_list error: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в get_chat_list_for_cleanup")

    return chats
