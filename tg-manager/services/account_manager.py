"""Telethon user account session management."""
from __future__ import annotations
import asyncio
import logging
from config import TG_API_ID, TG_API_HASH

log = logging.getLogger(__name__)

# In-memory pending clients (phone -> client) during login flow
_pending: dict[str, object] = {}

# Таймаут подключения в секундах
_CONNECT_TIMEOUT = 30


def _make_client(session_string: str = ""):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(
        StringSession(session_string),
        int(TG_API_ID),
        TG_API_HASH,
        connection_retries=1,
        timeout=_CONNECT_TIMEOUT,
    )


async def start_login(phone: str) -> str:
    """Начинает авторизацию по номеру телефона. Возвращает phone_code_hash."""
    from telethon.errors import FloodWaitError
    if not TG_API_ID or not TG_API_HASH:
        raise ValueError("TG_API_ID / TG_API_HASH не настроены. Укажите в переменных среды.")
    client = _make_client()
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client.send_code_request(phone)
    except FloodWaitError as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise
    _pending[phone] = client
    return result.phone_code_hash


async def confirm_code(phone: str, code: str, phone_code_hash: str):
    """Confirm SMS/TG code. Returns client or 'need_2fa'."""
    from telethon.errors import (
        PhoneCodeInvalidError, PhoneCodeExpiredError, SessionPasswordNeededError,
    )
    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        return client
    except SessionPasswordNeededError:
        return "need_2fa"
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        raise ValueError("Неверный или истёкший код.")


async def confirm_2fa(phone: str, password: str):
    """Complete 2FA login. Returns client."""
    from telethon.errors import PasswordHashInvalidError
    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        await client.sign_in(password=password)
        return client
    except PasswordHashInvalidError:
        raise ValueError("Неверный пароль 2FA.")


async def get_session_string(client) -> str:
    return client.session.save()


async def get_client_info_and_session(phone: str) -> tuple[str, dict]:
    """Get session string + user info from a pending login. Call after confirm_code/confirm_2fa."""
    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия не найдена — начните авторизацию заново.")
    session_str = client.session.save()
    me = await client.get_me()
    info = {
        "tg_user_id": me.id,
        "phone": me.phone or phone,
        "first_name": me.first_name or "",
        "username": me.username or "",
    }
    return session_str, info


async def cleanup_pending(phone: str) -> None:
    client = _pending.pop(phone, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_account_info(session_string: str) -> dict:
    client = _make_client(session_string)
    try:
        await client.connect()
        me = await client.get_me()
        return {
            "tg_user_id": me.id,
            "phone": me.phone or "",
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    finally:
        await client.disconnect()


async def get_dialogs(session_string: str, limit: int = 50) -> list[dict]:
    """Get user's channels and groups."""
    from telethon.tl.types import Channel, Chat
    client = _make_client(session_string)
    try:
        await client.connect()
        dialogs = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                dialogs.append({
                    "id": entity.id,
                    "title": entity.title,
                    "type": "channel" if isinstance(entity, Channel) and getattr(entity, "broadcast", False) else "group",
                    "members": getattr(entity, "participants_count", 0) or 0,
                    "username": getattr(entity, "username", "") or "",
                })
        return dialogs
    except Exception as e:
        log.exception("get_dialogs error: %s", e)
        return []
    finally:
        await client.disconnect()


async def send_message_via_account(session_string: str, chat_id: int, text: str) -> bool:
    client = _make_client(session_string)
    try:
        await client.connect()
        await client.send_message(chat_id, text)
        return True
    except Exception as e:
        log.exception("send_message error: %s", e)
        return False
    finally:
        await client.disconnect()


async def search_in_telegram(session_string: str, query: str, limit: int = 20) -> list[dict]:
    """Search Telegram contacts/global and return ordered results."""
    from telethon.tl.functions.contacts import SearchRequest
    client = _make_client(session_string)
    try:
        await client.connect()
        result = await client(SearchRequest(q=query, limit=limit))
        items = []
        for i, user in enumerate(result.users):
            items.append({
                "position": i + 1,
                "tg_user_id": user.id,
                "username": getattr(user, "username", "") or "",
                "first_name": getattr(user, "first_name", "") or "",
                "is_bot": getattr(user, "bot", False),
            })
        return items
    except Exception as e:
        log.exception("search_in_telegram error: %s", e)
        return []
    finally:
        await client.disconnect()
