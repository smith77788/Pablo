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
        result = await asyncio.wait_for(
            client.send_code_request(phone), timeout=_CONNECT_TIMEOUT
        )
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
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await client.get_me()
        return {
            "tg_user_id": me.id,
            "phone": me.phone or "",
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_dialogs(session_string: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """Возвращает каналы и группы аккаунта с поддержкой пагинации."""
    from telethon.tl.types import Channel, Chat
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        dialogs = []
        async for dialog in client.iter_dialogs(limit=limit, offset_id=offset):
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
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_message_via_account(session_string: str, chat_id: int, text: str) -> bool:
    """Отправляет сообщение через личный аккаунт. Возвращает True при успехе."""
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client.send_message(chat_id, text)
        return True
    except Exception as e:
        log.exception("send_message error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# Псевдоним для обратной совместимости с хендлером accounts.py
send_message = send_message_via_account


async def get_account_dialogs_stats(session_string: str) -> dict:
    """Возвращает статистику диалогов: всего, каналов, групп, личных чатов."""
    from telethon.tl.types import Channel, Chat, User
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        total = 0
        channels = 0
        groups = 0
        personal = 0
        async for dialog in client.iter_dialogs():
            total += 1
            entity = dialog.entity
            if isinstance(entity, Channel):
                if getattr(entity, "broadcast", False):
                    channels += 1
                else:
                    groups += 1
            elif isinstance(entity, Chat):
                groups += 1
            elif isinstance(entity, User):
                personal += 1
        return {
            "total": total,
            "channels": channels,
            "groups": groups,
            "personal": personal,
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def check_account_health(session_string: str) -> dict:
    """Проверяет доступность аккаунта: авторизован ли, не заблокирован ли.

    Возвращает {"ok": bool, "reason": str}.
    """
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await client.get_me()
        if me is None:
            return {"ok": False, "reason": "Аккаунт не авторизован или сессия истекла."}
        return {"ok": True, "reason": f"Аккаунт активен: {me.first_name or me.username or me.id}"}
    except Exception as e:
        err = str(e)
        if "AuthKeyUnregisteredError" in type(e).__name__ or "AUTH_KEY_UNREGISTERED" in err:
            return {"ok": False, "reason": "Сессия отозвана — требуется повторный вход."}
        if "UserDeactivatedBanError" in type(e).__name__ or "USER_DEACTIVATED_BAN" in err:
            return {"ok": False, "reason": "Аккаунт заблокирован Telegram."}
        if "UserDeactivatedError" in type(e).__name__ or "USER_DEACTIVATED" in err:
            return {"ok": False, "reason": "Аккаунт удалён или деактивирован."}
        log.exception("check_account_health error: %s", e)
        return {"ok": False, "reason": f"Ошибка проверки: {err[:200]}"}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_channel_members_count(session_string: str, channel_username: str) -> int:
    """Возвращает количество участников канала/группы по username. При ошибке — -1."""
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_username)
        count = getattr(entity, "participants_count", None)
        if count is None:
            # Для мегагрупп participants_count может быть None — запрашиваем напрямую
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await client(GetFullChannelRequest(entity))
            count = full.full_chat.participants_count
        return count if count is not None else -1
    except Exception as e:
        log.exception("get_channel_members_count error: %s", e)
        return -1
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_recent_messages(
    session_string: str,
    channel_username: str,
    limit: int = 5,
) -> list[dict]:
    """Возвращает последние сообщения из канала/группы.

    Каждый элемент: {"date": str, "text": str, "views": int}.
    Текст обрезается до 100 символов.
    """
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        messages = []
        async for msg in client.iter_messages(channel_username, limit=limit):
            text = (msg.text or msg.message or "").strip()
            if len(text) > 100:
                text = text[:100] + "…"
            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
            messages.append({
                "date": date_str,
                "text": text,
                "views": getattr(msg, "views", 0) or 0,
            })
        return messages
    except Exception as e:
        log.exception("get_recent_messages error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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
