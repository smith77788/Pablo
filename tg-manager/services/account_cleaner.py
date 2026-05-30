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

import asyncpg

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


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
    left = 0
    skipped = 0
    errors = []
    whitelist_set = set(str(x) for x in (whitelist or []))

    try:
        await asyncio.wait_for(client.connect(), timeout=15)

        dialogs = await client.get_dialogs(limit=500)
        chats = [d for d in dialogs if d.is_group or d.is_channel]

        for i, dialog in enumerate(chats):
            entity = dialog.entity
            entity_id = str(getattr(entity, "id", ""))
            username = getattr(entity, "username", "") or ""

            # Проверяем whitelist
            if entity_id in whitelist_set or username in whitelist_set:
                skipped += 1
                continue

            if progress_cb:
                try:
                    await progress_cb(i + 1, dialog.name or entity_id)
                except Exception:
                    log_exc_swallow(log, "Сбой progress_cb в leave_all_chats")

            if not dry_run:
                try:
                    from telethon.tl.functions.channels import LeaveChannelRequest
                    from telethon.tl.functions.messages import DeleteChatUserRequest
                    if dialog.is_channel or dialog.is_group:
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
    deleted = 0

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        from telethon.tl.functions.contacts import GetContactsRequest, DeleteContactsRequest

        result = await client(GetContactsRequest(hash=0))
        contacts = result.users if hasattr(result, "users") else []

        if contacts:
            user_ids = [u.id for u in contacts]
            await client(DeleteContactsRequest(id=user_ids))
            deleted = len(user_ids)

    except Exception as e:
        log.warning("delete_contacts error: %s", e)
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
    chats = []

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        dialogs = await client.get_dialogs(limit=200)

        for d in dialogs:
            entity = d.entity
            chat_type = "group" if d.is_group else ("channel" if d.is_channel else "pm")
            chats.append({
                "id": getattr(entity, "id", 0),
                "title": d.name or str(getattr(entity, "id", 0)),
                "username": getattr(entity, "username", None),
                "type": chat_type,
                "members": getattr(entity, "participants_count", None),
            })
    except Exception as e:
        log.warning("get_chat_list error: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в get_chat_list_for_cleanup")

    return chats
