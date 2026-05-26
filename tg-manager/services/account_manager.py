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
    except FloodWaitError:
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
                    "access_hash": getattr(entity, "access_hash", 0) or 0,
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


async def send_dm(session_string: str, username: str, text: str) -> dict:
    """Send a DM to a user by username or numeric ID.

    Returns {"ok": True} or {"error": "description", "flood_wait": seconds (optional)}.
    Handles common Telegram errors gracefully.
    """
    from telethon.errors import (
        UserPrivacyRestrictedError,
        FloodWaitError,
        PeerFloodError,
        UserIsBlockedError,
        ChatWriteForbiddenError,
        InputUserDeactivatedError,
        UsernameNotOccupiedError,
        UsernameInvalidError,
    )
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        target = username.lstrip("@") if isinstance(username, str) else username
        # Try to resolve numeric IDs
        if isinstance(target, str) and target.isdigit():
            target = int(target)
        await client.send_message(target, text)
        return {"ok": True}
    except FloodWaitError as e:
        return {"error": f"FloodWait: подождите {e.seconds}с", "flood_wait": e.seconds}
    except PeerFloodError:
        return {"error": "PeerFlood: аккаунт временно ограничен по рассылке"}
    except UserPrivacyRestrictedError:
        return {"error": "приватность: пользователь запретил входящие"}
    except UserIsBlockedError:
        return {"error": "заблокирован: вы в чёрном списке"}
    except ChatWriteForbiddenError:
        return {"error": "нет доступа к написанию"}
    except InputUserDeactivatedError:
        return {"error": "аккаунт удалён"}
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return {"error": "username не существует"}
    except Exception as e:
        return {"error": str(e)[:80]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
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


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL / GROUP OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

async def create_channel(
    session_string: str,
    title: str,
    about: str = "",
    megagroup: bool = False,
) -> dict:
    """Create a broadcast channel (megagroup=False) or supergroup (megagroup=True).

    Returns dict: {channel_id, title, username, type, invite_link, error?}
    """
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.tl.functions.messages import ExportChatInviteRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client(CreateChannelRequest(
            title=title,
            about=about,
            megagroup=megagroup,
            broadcast=not megagroup,
        ))
        ch = result.chats[0]
        invite_link = ""
        try:
            inv = await client(ExportChatInviteRequest(peer=ch))
            invite_link = getattr(inv, "link", "") or ""
        except Exception:
            pass
        return {
            "channel_id": ch.id,
            "title": ch.title,
            "username": getattr(ch, "username", "") or "",
            "type": "group" if megagroup else "channel",
            "invite_link": invite_link,
        }
    except Exception as e:
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            return {"error": f"FloodWait {e.seconds}с — Telegram ограничил создание", "flood_wait": e.seconds}
        log.exception("create_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def join_channel(session_string: str, invite_or_username: str) -> dict:
    """Join a channel or group by username (@name) or invite link (https://t.me/...).

    Returns dict: {title, members, channel_id, error?}
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        invite = invite_or_username.strip()
        if "t.me/+" in invite or "t.me/joinchat" in invite:
            # Private invite link
            hash_part = invite.split("/")[-1].lstrip("+")
            result = await client(ImportChatInviteRequest(hash=hash_part))
            ch = result.chats[0]
        else:
            username = invite.lstrip("@").lstrip("https://t.me/")
            entity = await client.get_entity(username)
            result = await client(JoinChannelRequest(channel=entity))
            ch = result.chats[0]
        return {
            "channel_id": ch.id,
            "title": ch.title,
            "members": getattr(ch, "participants_count", 0) or 0,
        }
    except Exception as e:
        log.exception("join_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def leave_channel(session_string: str, channel_id: int | str) -> bool:
    """Leave a channel/group by internal Telegram channel_id."""
    from telethon.tl.functions.channels import LeaveChannelRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(LeaveChannelRequest(channel=entity))
        return True
    except Exception as e:
        log.exception("leave_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def edit_channel_title(
    session_string: str, channel_id: int, title: str
) -> bool:
    from telethon.tl.functions.channels import EditTitleRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(EditTitleRequest(channel=entity, title=title))
        return True
    except Exception as e:
        log.exception("edit_channel_title error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def edit_channel_about(
    session_string: str, channel_id: int, about: str
) -> bool:
    from telethon.tl.functions.channels import EditAboutRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(EditAboutRequest(peer=entity, about=about))
        return True
    except Exception as e:
        log.exception("edit_channel_about error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def set_channel_username(
    session_string: str, channel_id: int, username: str
) -> str:
    """Set public username for channel. Returns '' on success, error string on failure."""
    from telethon.tl.functions.channels import UpdateUsernameRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(UpdateUsernameRequest(channel=entity, username=username.lstrip("@")))
        return ""
    except Exception as e:
        log.exception("set_channel_username error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_channel_invite_link(session_string: str, channel_id: int) -> str:
    """Get (or create) an invite link for the channel. Returns link string or ''."""
    from telethon.tl.functions.messages import ExportChatInviteRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        result = await client(ExportChatInviteRequest(peer=entity))
        return getattr(result, "link", "") or ""
    except Exception as e:
        log.exception("get_channel_invite_link error: %s", e)
        return ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def delete_channel(session_string: str, channel_id: int) -> bool:
    """Permanently delete a channel or group. Irreversible."""
    from telethon.tl.functions.channels import DeleteChannelRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(DeleteChannelRequest(channel=entity))
        return True
    except Exception as e:
        log.exception("delete_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_channel_members(
    session_string: str, channel_id: int, limit: int = 50
) -> list[dict]:
    """Return list of channel/group members (up to limit)."""
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsRecent
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        result = await client(GetParticipantsRequest(
            channel=entity,
            filter=ChannelParticipantsRecent(),
            offset=0,
            limit=limit,
            hash=0,
        ))
        members = []
        for user in result.users:
            members.append({
                "user_id": user.id,
                "username": getattr(user, "username", "") or "",
                "first_name": getattr(user, "first_name", "") or "",
                "is_bot": getattr(user, "bot", False),
            })
        return members
    except Exception as e:
        log.exception("get_channel_members error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def invite_users_to_channel(
    session_string: str, channel_id: int, usernames: list[str]
) -> dict:
    """Invite a list of users (@username or phone) to a group.

    Returns {invited: int, failed: list[str]}.
    """
    from telethon.tl.functions.channels import InviteToChannelRequest
    client = _make_client(session_string)
    invited = 0
    failed = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        for username in usernames:
            try:
                user = await client.get_entity(username.strip())
                await client(InviteToChannelRequest(channel=entity, users=[user]))
                invited += 1
                await asyncio.sleep(1)
            except Exception as e:
                failed.append(f"{username}: {str(e)[:60]}")
        return {"invited": invited, "failed": failed}
    except Exception as e:
        log.exception("invite_users_to_channel error: %s", e)
        return {"invited": invited, "failed": failed + [str(e)[:100]]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_contacts(session_string: str) -> list[dict]:
    """Fetch contacts list from a Telegram account.

    Returns list of {user_id, username, phone, first_name, last_name}.
    Bots and deleted accounts are excluded.
    """
    from telethon.tl.functions.contacts import GetContactsRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client(GetContactsRequest(hash=0))
        contacts = []
        for user in result.users:
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            contacts.append({
                "user_id": user.id,
                "username": getattr(user, "username", "") or "",
                "phone": getattr(user, "phone", "") or "",
                "first_name": getattr(user, "first_name", "") or "",
                "last_name": getattr(user, "last_name", "") or "",
            })
        return contacts
    except Exception as e:
        log.warning("get_contacts error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def kick_from_channel(
    session_string: str, channel_id: int, user_id: int
) -> bool:
    """Kick (ban + unban) a user from a channel/group."""
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        channel = await client.get_entity(channel_id)
        user = await client.get_entity(user_id)
        # Ban
        banned = ChatBannedRights(until_date=None, view_messages=True)
        await client(EditBannedRequest(channel=channel, participant=user, banned_rights=banned))
        await asyncio.sleep(1)
        # Unban (kick, not permanent ban)
        unbanned = ChatBannedRights(until_date=None)
        await client(EditBannedRequest(channel=channel, participant=user, banned_rights=unbanned))
        return True
    except Exception as e:
        log.exception("kick_from_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════

async def post_to_channel(
    session_string: str, channel_id: int | str, text: str, access_hash: int = 0
) -> dict:
    """Post a text message to a channel/group.

    access_hash: if provided, uses InputPeerChannel directly (fast, no cache needed).
    Without access_hash and without @username, fetches dialogs to populate entity cache.

    Returns {"msg_id": int} on success or {"error": str, "flood_wait"?: int} on failure.
    """
    from telethon.tl.types import InputPeerChannel
    from telethon.errors import FloodWaitError, ChatWriteForbiddenError, UserNotParticipantError
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve peer — 3 strategies in order of speed:
        # 1. InputPeerChannel with access_hash (no API call needed)
        # 2. @username string (single API call)
        # 3. Populate entity cache via get_dialogs (slow but reliable)
        if access_hash and isinstance(channel_id, int) and channel_id > 0:
            peer = InputPeerChannel(channel_id=channel_id, access_hash=access_hash)
        elif isinstance(channel_id, str) and not channel_id.lstrip("-").isdigit():
            peer = channel_id  # @username — Telethon resolves via API
        else:
            cid = abs(int(channel_id)) if isinstance(channel_id, str) else abs(channel_id)
            async for _d in client.iter_dialogs(limit=500):
                if getattr(_d.entity, "id", None) == cid:
                    peer = InputPeerChannel(
                        channel_id=cid,
                        access_hash=getattr(_d.entity, "access_hash", 0),
                    )
                    break
            else:
                return {"error": "Канал не найден в диалогах аккаунта"}

        msg = await client.send_message(peer, text, parse_mode="html")
        return {"msg_id": msg.id}
    except FloodWaitError as e:
        return {"error": f"Флуд-лимит: подождите {e.seconds}с", "flood_wait": e.seconds}
    except ChatWriteForbiddenError:
        return {"error": "Нет прав для публикации в этом канале"}
    except UserNotParticipantError:
        return {"error": "Аккаунт не является участником канала"}
    except Exception as e:
        log.exception("post_to_channel error: %s", e)
        return {"error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_reaction(
    session_string: str, channel_id: int, msg_id: int, emoji: str
) -> bool:
    """Send a reaction emoji to a specific message."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(SendReactionRequest(
            peer=entity,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=emoji)],
        ))
        return True
    except Exception as e:
        log.exception("send_reaction error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def report_peer(
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
) -> bool:
    """Report a channel/user to Telegram moderators.

    reason: 'spam' | 'violence' | 'pornography' | 'childabuse' | 'copyright' | 'other'
    """
    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence,
        InputReportReasonPornography, InputReportReasonChildAbuse,
        InputReportReasonCopyright, InputReportReasonOther,
    )
    reason_map = {
        "spam": InputReportReasonSpam(),
        "violence": InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse": InputReportReasonChildAbuse(),
        "copyright": InputReportReasonCopyright(),
        "other": InputReportReasonOther(),
    }
    tg_reason = reason_map.get(reason, InputReportReasonSpam())
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        await client(ReportPeerRequest(peer=entity, reason=tg_reason, message=message))
        return True
    except Exception as e:
        log.exception("report_peer error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT PROFILE
# ══════════════════════════════════════════════════════════════════════════════

async def update_profile(
    session_string: str,
    first_name: str | None = None,
    last_name: str | None = None,
    about: str | None = None,
) -> bool:
    """Update the connected account's profile. Pass None to leave a field unchanged."""
    from telethon.tl.functions.account import UpdateProfileRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client.get_me()
        kwargs: dict = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if about is not None:
            kwargs["about"] = about
        if not kwargs:
            return True
        await client(UpdateProfileRequest(**kwargs))
        return True
    except Exception as e:
        log.exception("update_profile error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def update_account_username(session_string: str, username: str) -> str:
    """Update account username. Returns '' on success, error string on failure."""
    from telethon.tl.functions.account import UpdateUsernameRequest
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client(UpdateUsernameRequest(username=username.lstrip("@")))
        return ""
    except Exception as e:
        log.exception("update_account_username error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# BOTFATHER BOT CREATION
# ══════════════════════════════════════════════════════════════════════════════

_BOTFATHER_USERNAME = "BotFather"


async def create_bot_via_botfather(
    session_string: str,
    bot_display_name: str,
    bot_username: str,
) -> dict:
    """Create a new Telegram bot via @BotFather automated dialog.

    Returns dict with 'token' and 'username' on success,
    or 'error' key with message on failure.
    """
    import re
    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        async def _bf_send(text: str) -> str:
            """Send message to BotFather and return its response text."""
            await client.send_message(_BOTFATHER_USERNAME, text)
            await asyncio.sleep(3)
            msgs = await client.get_messages(_BOTFATHER_USERNAME, limit=1)
            return msgs[0].text if msgs else ""

        # Step 1: start fresh
        resp = await _bf_send("/newbot")
        if "name" not in resp.lower() and "Alright" not in resp:
            # May be in a previous incomplete flow — cancel first
            await _bf_send("/cancel")
            await asyncio.sleep(1)
            resp = await _bf_send("/newbot")

        # Step 2: send display name
        resp = await _bf_send(bot_display_name)

        # Check for username prompt
        if "username" not in resp.lower():
            return {"error": f"Unexpected BotFather response after name: {resp[:200]}"}

        # Step 3: send username
        uname = bot_username.lstrip("@")
        if not uname.endswith("bot") and not uname.endswith("Bot"):
            uname = uname + "bot"
        resp = await _bf_send(uname)

        # Extract token (format: 123456789:AAABBBCCC...)
        token_match = re.search(r"\b(\d{8,12}:[A-Za-z0-9_-]{35,})\b", resp)
        if not token_match:
            return {"error": f"Token not found in BotFather response: {resp[:300]}"}

        token = token_match.group(1)
        return {
            "token": token,
            "username": uname,
            "display_name": bot_display_name,
        }
    except Exception as e:
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            return {"error": f"FloodWait {e.seconds}с — Telegram ограничил создание", "flood_wait": e.seconds}
        log.exception("create_bot_via_botfather error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
