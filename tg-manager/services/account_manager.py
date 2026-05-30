"""Telethon user account session management."""
from __future__ import annotations
import asyncio
import logging
import random
from config import TG_API_ID, TG_API_HASH, TG_PROXY

log = logging.getLogger(__name__)


def _parse_proxy(proxy_url: str):
    """Parse socks5://user:pass@host:port → (socks.SOCKS5, host, port, True, user, pass).
    Returns None if proxy_url is empty.
    """
    if not proxy_url:
        return None
    try:
        import socks
        url = proxy_url.strip()
        if "://" in url:
            url = url.split("://", 1)[1]
        user, password = None, None
        if "@" in url:
            creds, hostpart = url.rsplit("@", 1)
            if ":" in creds:
                user, password = creds.split(":", 1)
            else:
                user = creds
        else:
            hostpart = url
        host, port = hostpart.rsplit(":", 1)
        return (socks.SOCKS5, host, int(port), True, user, password)
    except Exception as e:
        log.warning("Failed to parse TG_PROXY %r: %s — running without proxy", proxy_url, e)
        return None

# In-memory pending clients (phone -> client) during login flow
_pending: dict[str, object] = {}

# Device fingerprints for pending phone logins (phone -> device dict)
_pending_device: dict[str, dict] = {}

# QR login sessions: user_id -> (client, qr_login_object, device dict)
_pending_qr: dict[int, tuple] = {}

# Таймаут подключения в секундах
_CONNECT_TIMEOUT = 30
# Таймаут на отдельные Telethon операции (get_entity, send_message и т.д.)
_OP_TIMEOUT = 45
# Кап для FloodWait backoff
_FLOOD_CAP = 65.0


def _backoff(attempt: int, base: float = 2.0, cap: float = 120.0) -> float:
    """Return exponential backoff seconds: base^attempt, capped at cap."""
    import math
    return min(base ** attempt, cap)


# Pool of realistic Android device fingerprints
_ANDROID_DEVICES: list[tuple[str, str]] = [
    ("Samsung SM-S928B", "Android 14"),
    ("Samsung SM-S918B", "Android 14"),
    ("Samsung SM-S911B", "Android 14"),
    ("Samsung SM-A546B", "Android 13"),
    ("Xiaomi 14 Pro",    "Android 14"),
    ("Xiaomi 13T Pro",   "Android 13"),
    ("Xiaomi Redmi Note 13 Pro", "Android 13"),
    ("Google Pixel 8 Pro", "Android 14"),
    ("Google Pixel 7a",    "Android 13"),
    ("OnePlus 12",         "Android 14"),
    ("OnePlus 11",         "Android 13"),
    ("POCO X6 Pro",        "Android 14"),
    ("realme GT 5 Pro",    "Android 14"),
    ("Motorola Edge 50 Pro", "Android 14"),
    ("Samsung SM-A336B",   "Android 12"),
    ("Xiaomi POCO M5s",    "Android 12"),
    ("Samsung SM-A135F",   "Android 13"),
    ("Vivo V27 Pro",       "Android 13"),
    ("Nokia G60 5G",       "Android 12"),
    ("Motorola Moto G84",  "Android 13"),
]
_APP_VERSIONS: list[str] = [
    "10.14.4", "10.14.3", "10.13.2", "10.12.2", "10.11.0",
    "10.10.1", "10.9.1",  "10.8.2",  "10.7.0",  "10.6.2",
]


def generate_device_fingerprint() -> dict:
    """Return a random realistic Android device fingerprint."""
    import random
    device_model, system_version = random.choice(_ANDROID_DEVICES)
    return {
        "device_model": device_model,
        "system_version": system_version,
        "app_version": random.choice(_APP_VERSIONS),
    }


def _make_client(session_string: str = "", device: dict | None = None):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    d = device or {}
    # Per-account proxy overrides global TG_PROXY env var
    acc_proxy_url = d.get("proxy_url") or ""
    proxy = _parse_proxy(acc_proxy_url) if acc_proxy_url else _parse_proxy(TG_PROXY)
    return TelegramClient(
        StringSession(session_string),
        int(TG_API_ID),
        TG_API_HASH,
        device_model=d.get("device_model") or "Samsung SM-S911B",
        system_version=d.get("system_version") or "Android 14",
        app_version=d.get("app_version") or "10.9.1",
        lang_code="ru",
        system_lang_code="ru-RU",
        connection_retries=3,
        timeout=_CONNECT_TIMEOUT,
        flood_sleep_threshold=0,
        proxy=proxy,
    )


async def start_login(phone: str) -> tuple[str, str]:
    """Начинает авторизацию по номеру телефона.
    Возвращает (phone_code_hash, delivery_hint) где delivery_hint — строка о способе доставки.
    """
    from telethon.errors import FloodWaitError
    if not TG_API_ID or not TG_API_HASH:
        raise ValueError("TG_API_ID / TG_API_HASH не настроены. Укажите в переменных среды.")
    device = generate_device_fingerprint()
    _pending_device[phone] = device
    client = _make_client("", device)
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

    # Determine where the code was sent so handlers can tell the user
    type_name = type(result.type).__name__ if result.type else ""
    if "App" in type_name:
        delivery_hint = "📱 Код отправлен в приложение Telegram"
    elif "Sms" in type_name:
        delivery_hint = "💬 Код отправлен по SMS"
    elif "Call" in type_name or "Flash" in type_name:
        delivery_hint = "📞 Код придёт звонком на номер"
    else:
        delivery_hint = "📱 Код отправлен (проверьте приложение Telegram или SMS)"

    return result.phone_code_hash, delivery_hint


async def resend_code(phone: str, phone_code_hash: str) -> tuple[str, str]:
    """Resend code via next available method (usually SMS if app was first).
    Returns (new_phone_code_hash, delivery_hint).
    """
    from telethon.tl.functions.auth import ResendCodeRequest
    from telethon.errors import FloodWaitError
    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        result = await asyncio.wait_for(
            client(ResendCodeRequest(phone_number=phone, phone_code_hash=phone_code_hash)),
            timeout=_CONNECT_TIMEOUT,
        )
    except FloodWaitError:
        raise
    type_name = type(result.type).__name__ if result.type else ""
    if "Sms" in type_name:
        hint = "💬 Код отправлен по SMS"
    elif "Call" in type_name or "Flash" in type_name:
        hint = "📞 Код придёт звонком"
    elif "App" in type_name:
        hint = "📱 Код отправлен в приложение Telegram"
    else:
        hint = "💬 Код выслан повторно (SMS или звонок)"
    return result.phone_code_hash, hint


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


# ── Session Import Helpers ─────────────────────────────────────────────────────

async def import_from_session_string(session_string: str) -> tuple[str, dict]:
    """Validate a Telethon StringSession and return (session_str, info).
    Raises ValueError if the session is invalid or unauthorized.
    """
    session_string = session_string.strip()
    if not session_string or len(session_string) < 20:
        raise ValueError("Строка сессии слишком короткая.")

    client = _make_client(session_string)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            raise ValueError("Сессия не авторизована или истекла.")
        me = await client.get_me()
        info = {
            "tg_user_id": me.id,
            "phone": getattr(me, "phone", "") or f"id:{me.id}",
            "first_name": getattr(me, "first_name", "") or "",
            "username": getattr(me, "username", "") or "",
        }
        return session_string, info
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def import_from_pyrogram_json(json_str: str) -> tuple[str, dict]:
    """Convert a Pyrogram JSON session to Telethon StringSession.

    Accepted JSON fields: dc_id, auth_key (base64), user_id (optional).
    Converts auth_key + dc_id to a Telethon StringSession and validates it.
    """
    import json as _json
    import struct
    import base64
    from ipaddress import IPv4Address
    from telethon.sessions import StringSession

    try:
        data = _json.loads(json_str)
    except Exception:
        raise ValueError("Некорректный JSON. Проверьте формат.")

    dc_id = int(data.get("dc_id") or 2)
    auth_key_raw = data.get("auth_key", "")
    if not auth_key_raw:
        raise ValueError("Поле auth_key не найдено в JSON.")

    try:
        auth_key = base64.b64decode(auth_key_raw + "==")
    except Exception:
        raise ValueError("Не удалось декодировать auth_key (ожидается base64).")

    if len(auth_key) != 256:
        raise ValueError(f"Неверная длина auth_key: {len(auth_key)}, нужно 256 байт.")

    # Production DC server IPs
    DC_IPS: dict[int, str] = {
        1: "149.154.175.53",
        2: "149.154.167.51",
        3: "149.154.175.100",
        4: "149.154.167.91",
        5: "91.108.56.130",
    }
    ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed
    packed = struct.pack(">B4sH256s", dc_id, ip_bytes, 443, auth_key)
    session_string = "1" + base64.urlsafe_b64encode(packed).decode()

    return await import_from_session_string(session_string)


async def import_from_tdata(tdata_path: str) -> tuple[str, dict]:
    """Convert a TDesktop tdata directory to Telethon StringSession via opentele."""
    try:
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
        from telethon.sessions import StringSession as _SS
    except ImportError:
        raise ImportError(
            "Пакет opentele не установлен. Обратитесь к администратору.\n"
            "pip install opentele"
        )

    try:
        td = TDesktop(tdata_path)
    except Exception as e:
        raise ValueError(f"Не удалось загрузить tdata: {e}")

    if not td.isLoaded():
        raise ValueError("tdata не загружены. Проверьте архив — должна быть папка tdata с файлом key_datas.")

    try:
        client = await td.ToTelethon(session=_SS(), flag=UseCurrentSession)
    except Exception as e:
        raise ValueError(f"Ошибка конвертации tdata → Telethon: {e}")

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        if not await client.is_user_authorized():
            raise ValueError("Сессия из tdata не авторизована.")
        session_str = client.session.save()
        me = await client.get_me()
        info = {
            "tg_user_id": me.id,
            "phone": getattr(me, "phone", "") or f"id:{me.id}",
            "first_name": getattr(me, "first_name", "") or "",
            "username": getattr(me, "username", "") or "",
        }
        return session_str, info
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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
        **_pending_device.get(phone, {}),
    }
    return session_str, info


async def cleanup_pending(phone: str) -> None:
    _pending_device.pop(phone, None)
    client = _pending.pop(phone, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── QR Login ──────────────────────────────────────────────────────────────────

async def start_qr_login(user_id: int) -> bytes:
    """Start QR code login. Returns PNG image bytes.

    Keeps a connected client in _pending_qr[user_id].
    Call wait_qr_login() in a background task to detect scan.
    """
    import io
    import qrcode  # type: ignore

    await cleanup_qr_pending(user_id)

    device = generate_device_fingerprint()
    client = _make_client("", device)
    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
    qr = await client.qr_login()
    _pending_qr[user_id] = (client, qr, device)

    img = qrcode.make(qr.url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def wait_qr_login(user_id: int, timeout: float = 120.0) -> tuple[str, dict]:
    """Block until user scans QR code or timeout. Returns (session_str, info).

    Raises asyncio.TimeoutError if not scanned in time.
    Raises SessionPasswordNeededError if account requires 2FA.
    """
    from telethon.errors import SessionPasswordNeededError

    entry = _pending_qr.get(user_id)
    if not entry:
        raise ValueError("QR сессия не найдена — начните заново.")
    client, qr, device = entry
    try:
        await asyncio.wait_for(qr.wait(), timeout=timeout)
    except SessionPasswordNeededError:
        # Caller must handle 2FA separately; client stays in _pending_qr
        raise

    me = await client.get_me()
    session_str = client.session.save()
    info = {
        "tg_user_id": me.id,
        "phone": getattr(me, "phone", "") or f"id:{me.id}",
        "first_name": getattr(me, "first_name", "") or "",
        "username": getattr(me, "username", "") or "",
        **device,
    }
    return session_str, info


async def confirm_qr_2fa(user_id: int, password: str) -> tuple[str, dict]:
    """Finish QR login that required 2FA. Returns (session_str, info)."""
    from telethon.errors import PasswordHashInvalidError

    entry = _pending_qr.get(user_id)
    if not entry:
        raise ValueError("QR сессия не найдена — начните заново.")
    client, _, device = entry
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        raise ValueError("Неверный пароль 2FA.")

    me = await client.get_me()
    session_str = client.session.save()
    info = {
        "tg_user_id": me.id,
        "phone": getattr(me, "phone", "") or f"id:{me.id}",
        "first_name": getattr(me, "first_name", "") or "",
        "username": getattr(me, "username", "") or "",
        **device,
    }
    return session_str, info


async def cleanup_qr_pending(user_id: int) -> None:
    entry = _pending_qr.pop(user_id, None)
    if entry:
        client, *_ = entry
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_account_info(session_string: str, _acc: dict | None = None) -> dict:
    client = _make_client(session_string, _acc)
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


async def get_dialogs(session_string: str, limit: int = 50, offset: int = 0,
                      _acc: dict | None = None) -> list[dict]:
    """Возвращает каналы и группы аккаунта с поддержкой пагинации."""
    from telethon.tl.types import Channel, Chat
    client = _make_client(session_string, _acc)
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


async def scan_owned_assets(
    session_string: str, _acc: dict | None = None
) -> dict:
    """Scan account for channels/groups where it's admin or creator.

    Returns {'channels': [...], 'groups': [...], 'error': str|None}
    Each item: {id, title, username, members, is_creator, access_hash}
    """
    from telethon.tl.types import Channel, Chat
    client = _make_client(session_string, _acc)
    channels: list[dict] = []
    groups: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        async def _collect():
            _ch, _gr = [], []
            async for dialog in client.iter_dialogs(limit=300):
                entity = dialog.entity
                if isinstance(entity, Channel):
                    is_creator = getattr(entity, "creator", False)
                    admin_rights = getattr(entity, "admin_rights", None)
                    if not (is_creator or admin_rights is not None):
                        continue
                    is_broadcast = getattr(entity, "broadcast", False)
                    item = {
                        "id": entity.id,
                        "title": entity.title or "",
                        "username": getattr(entity, "username", "") or "",
                        "members": getattr(entity, "participants_count", 0) or 0,
                        "is_creator": is_creator,
                        "access_hash": getattr(entity, "access_hash", 0) or 0,
                    }
                    if is_broadcast:
                        _ch.append(item)
                    else:
                        _gr.append(item)
            return _ch, _gr

        channels, groups = await asyncio.wait_for(_collect(), timeout=_OP_TIMEOUT)
        return {"channels": channels, "groups": groups, "error": None}
    except Exception as e:
        err_str = str(e)
        err_low = err_str.lower()
        _is_session = any(x in err_low for x in (
            "auth", "authkey", "unauthorized", "key is not registered",
            "registered in the system", "auth_key",
        ))
        if _is_session:
            log.warning("scan_owned_assets session dead: %s", e)
        else:
            log.exception("scan_owned_assets error: %s", e)
        return {"channels": [], "groups": [], "error": err_str[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def send_message_via_account(session_string: str, chat_id: int, text: str,
                                   _acc: dict | None = None) -> bool:
    """Отправляет сообщение через личный аккаунт. Возвращает True при успехе."""
    client = _make_client(session_string, _acc)
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


async def send_dm(session_string: str, username: str, text: str,
                  _acc: dict | None = None) -> dict:
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
    client = _make_client(session_string, _acc)
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
    except PeerFloodError as e:
        return {"error": f"PeerFlood: аккаунт временно ограничен по рассылке: {e}", "banned": True}
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


async def get_account_dialogs_stats(session_string: str, _acc: dict | None = None) -> dict:
    """Возвращает статистику диалогов: всего, каналов, групп, личных чатов."""
    from telethon.tl.types import Channel, Chat, User
    client = _make_client(session_string, _acc)
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


async def check_account_health(session_string: str, _acc: dict | None = None) -> dict:
    """Проверяет доступность аккаунта: авторизован ли, не заблокирован ли.

    Возвращает {"ok": bool, "reason": str}.
    """
    result = await check_account_status_full(session_string, _acc=_acc, check_spambot=False)
    return {"ok": result["status"] == "active", "reason": result["reason"]}


async def check_account_status_full(
    session_string: str,
    _acc: dict | None = None,
    check_spambot: bool = True,
) -> dict:
    """Детальная проверка состояния аккаунта.

    Возвращает {
        "status": "active"|"cooldown"|"spamblock"|"banned"|"deactivated"|"session_expired",
        "reason": str,
        "display_name": str,
    }
    """
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        if me is None:
            return {"status": "session_expired", "reason": "Аккаунт не авторизован или сессия истекла.", "display_name": ""}

        display_name = me.first_name or (f"@{me.username}" if me.username else str(me.id))

        if not check_spambot:
            return {"status": "active", "reason": f"Аккаунт активен", "display_name": display_name}

        # Check SpamBot for spamblock detection
        try:
            from telethon.tl.types import User
            spam_bot = await asyncio.wait_for(client.get_entity("@SpamBot"), timeout=8.0)
            await asyncio.wait_for(
                client.send_message(spam_bot, "/start"),
                timeout=8.0,
            )
            await asyncio.sleep(2.5)
            msgs = await asyncio.wait_for(
                client.get_messages(spam_bot, limit=1),
                timeout=8.0,
            )
            if msgs:
                reply_text = msgs[0].text or ""
                reply_lower = reply_text.lower()
                if any(kw in reply_lower for kw in ("no limits", "no complaints", "good standing",
                                                      "нет ограничений", "нет жалоб", "не было жалоб")):
                    return {"status": "active", "reason": "Аккаунт активен, ограничений нет", "display_name": display_name}
                if any(kw in reply_lower for kw in ("limited", "spam", "restricted", "ограничен", "спам", "ограничения")):
                    return {"status": "spamblock", "reason": f"SpamBot: {reply_text[:120]}", "display_name": display_name}
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass

        return {"status": "active", "reason": "Аккаунт активен", "display_name": display_name}

    except Exception as e:
        err = str(e)
        etype = type(e).__name__
        err_low = err.lower()
        if ("AuthKeyUnregisteredError" in etype or "AUTH_KEY_UNREGISTERED" in err
                or "key is not registered" in err_low or "registered in the system" in err_low):
            return {"status": "session_expired", "reason": "Ключ сессии отозван Telegram — требуется переавторизация.", "display_name": ""}
        if "UserDeactivatedBanError" in etype or "USER_DEACTIVATED_BAN" in err:
            return {"status": "banned", "reason": "Аккаунт заблокирован Telegram.", "display_name": ""}
        if "UserDeactivatedError" in etype or "USER_DEACTIVATED" in err:
            return {"status": "deactivated", "reason": "Аккаунт удалён или деактивирован.", "display_name": ""}
        if "FloodWaitError" in etype or "FLOOD_WAIT" in err:
            return {"status": "cooldown", "reason": f"FloodWait: {err[:80]}", "display_name": ""}
        if "PeerFloodError" in etype or "PEER_FLOOD" in err:
            return {"status": "spamblock", "reason": "PeerFlood — массовые ограничения.", "display_name": ""}
        log.exception("check_account_status_full error: %s", e)
        return {"status": "active", "reason": f"Нет данных: {err[:120]}", "display_name": ""}


async def get_channel_members_count(session_string: str, channel_username: str,
                                    _acc: dict | None = None) -> int:
    """Возвращает количество участников канала/группы по username. При ошибке — -1."""
    client = _make_client(session_string, _acc)
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


async def get_full_channel_info(
    session_string: str,
    channel_id: int | str,
    _acc: dict | None = None,
) -> dict | None:
    """Возвращает {'about', 'members_count', 'username', 'title'} для канала/группы."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id)
        full = await client(GetFullChannelRequest(entity))
        about = getattr(full.full_chat, "about", "") or ""
        members = getattr(full.full_chat, "participants_count", 0) or 0
        return {
            "about": about,
            "members_count": members,
            "username": getattr(entity, "username", "") or "",
            "title": getattr(entity, "title", "") or "",
        }
    except Exception as e:
        log.debug("get_full_channel_info error: %s", e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_recent_messages(
    session_string: str,
    channel_username: str,
    limit: int = 5,
    _acc: dict | None = None,
) -> list[dict]:
    """Возвращает последние сообщения из канала/группы.

    Каждый элемент: {"date": str, "text": str, "views": int}.
    Текст обрезается до 100 символов.
    """
    client = _make_client(session_string, _acc)
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


async def search_in_telegram(session_string: str, query: str, limit: int = 20,
                             _acc: dict | None = None) -> list[dict]:
    """Search Telegram contacts/global and return ordered results."""
    from telethon.tl.functions.contacts import SearchRequest
    client = _make_client(session_string, _acc)
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
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            raise
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
    _acc: dict | None = None,
) -> dict:
    """Create a broadcast channel (megagroup=False) or supergroup (megagroup=True).

    Returns dict: {channel_id, title, username, type, invite_link, error?}
    """
    from telethon.tl.functions.channels import CreateChannelRequest
    from telethon.tl.functions.messages import ExportChatInviteRequest
    client = _make_client(session_string, _acc)
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
        from telethon.errors import FloodWaitError, PeerFloodError
        if isinstance(e, FloodWaitError):
            return {"error": f"FloodWait {e.seconds}с — Telegram ограничил создание", "flood_wait": e.seconds}
        if isinstance(e, PeerFloodError):
            return {"error": f"PeerFlood: аккаунт ограничен — {e}", "flood_wait": e.seconds if hasattr(e, 'seconds') else 0}
        log.exception("create_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def join_channel(session_string: str, invite_or_username: str,
                       _acc: dict | None = None) -> dict:
    """Join a channel or group by username (@name) or invite link (https://t.me/...).

    Returns dict: {title, members, channel_id, error?}
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
    client = _make_client(session_string, _acc)
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
        from telethon.errors import FloodWaitError, UserBannedInChannelError, ChannelPrivateError, PeerFloodError
        if isinstance(e, FloodWaitError):
            return {"error": f"FloodWait {e.seconds}с — подождите перед вступлением", "flood_wait": e.seconds}
        if isinstance(e, UserBannedInChannelError):
            return {"error": f"Аккаунт забанен в этом канале: {e}", "banned": True}
        if isinstance(e, ChannelPrivateError):
            return {"error": f"Канал приватный или аккаунт заблокирован: {e}", "banned": True}
        if isinstance(e, PeerFloodError):
            return {"error": f"PeerFlood: аккаунт временно ограничен: {e}", "banned": True}
        log.exception("join_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def leave_channel(session_string: str, channel_id: int | str,
                        _acc: dict | None = None) -> bool:
    """Leave a channel/group by internal Telegram channel_id."""
    from telethon.tl.functions.channels import LeaveChannelRequest
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(LeaveChannelRequest(channel=entity))
        return True
    except Exception as e:
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            log.warning("leave_channel FloodWait %ds", e.seconds)
            raise
        log.exception("leave_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def edit_channel_title(
    session_string: str, channel_id: int, title: str, _acc: dict | None = None,
) -> bool:
    from telethon.tl.functions.channels import EditTitleRequest
    client = _make_client(session_string, _acc)
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
    session_string: str, channel_id: int, about: str, _acc: dict | None = None,
) -> bool:
    from telethon.tl.functions.channels import EditAboutRequest
    client = _make_client(session_string, _acc)
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
    session_string: str, channel_id: int, username: str, _acc: dict | None = None,
) -> str:
    """Set public username for channel. Returns '' on success, error string on failure."""
    from telethon.tl.functions.channels import UpdateUsernameRequest
    client = _make_client(session_string, _acc)
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


async def get_channel_invite_link(session_string: str, channel_id: int,
                                  _acc: dict | None = None) -> str:
    """Get (or create) an invite link for the channel. Returns link string or ''."""
    from telethon.tl.functions.messages import ExportChatInviteRequest
    client = _make_client(session_string, _acc)
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


async def delete_channel(session_string: str, channel_id: int,
                         _acc: dict | None = None) -> bool:
    """Permanently delete a channel or group. Irreversible."""
    from telethon.tl.functions.channels import DeleteChannelRequest
    client = _make_client(session_string, _acc)
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
    session_string: str, channel_id: int, limit: int = 50, _acc: dict | None = None,
) -> list[dict]:
    """Return list of channel/group members (up to limit)."""
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsRecent
    client = _make_client(session_string, _acc)
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
    session_string: str, channel_id: int, usernames: list[str], _acc: dict | None = None,
    access_hash: int = 0,
) -> dict:
    """Invite a list of users (@username or phone) to a channel/supergroup.

    Returns {invited: int, failed: list[str], error?: str}.
    Uses access_hash for reliable entity resolution without dialog cache.
    """
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.tl.types import InputPeerChannel
    from telethon.errors import (
        FloodWaitError, PeerFloodError, UserBannedInChannelError,
        ChatAdminRequiredError, UserPrivacyRestrictedError,
        UserNotMutualContactError, UserChannelsTooMuchError,
    )
    from services import session_simulator
    client = _make_client(session_string, _acc)
    invited = 0
    failed = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve channel entity — use access_hash when available (fastest, no cache needed)
        if access_hash and isinstance(channel_id, int) and channel_id > 0:
            channel_peer = InputPeerChannel(channel_id=channel_id, access_hash=access_hash)
        else:
            # Fallback: try to get entity (requires entity in Telethon cache)
            try:
                channel_peer = await client.get_entity(channel_id)
            except Exception:
                # Last resort: iterate dialogs to find the channel
                channel_peer = None
                async for dlg in client.iter_dialogs(limit=300):
                    eid = getattr(dlg.entity, "id", None)
                    if eid and abs(eid) == abs(int(channel_id)):
                        ah = getattr(dlg.entity, "access_hash", 0)
                        channel_peer = InputPeerChannel(channel_id=abs(eid), access_hash=ah)
                        break
                if not channel_peer:
                    return {"invited": 0, "failed": [], "error": f"Канал {channel_id} не найден в диалогах аккаунта"}

        for idx, username in enumerate(usernames):
            try:
                user = await client.get_entity(username.strip())
                await client(InviteToChannelRequest(channel=channel_peer, users=[user]))
                invited += 1
                # Human-like delay between invites
                if idx < len(usernames) - 1:
                    await asyncio.sleep(random.uniform(3, 15) * session_simulator.chaos_factor())
            except ChatAdminRequiredError:
                # Account has no invite_users right — no point continuing
                return {
                    "invited": invited, "failed": failed,
                    "error": "Нет прав администратора для инвайта. Убедитесь что аккаунт назначен администратором с правом 'Добавление участников'.",
                }
            except PeerFloodError:
                return {"invited": invited, "failed": failed,
                        "error": "PeerFlood — аккаунт временно ограничен Telegram"}
            except UserBannedInChannelError:
                failed.append(f"{username}: забанен в канале")
            except (UserPrivacyRestrictedError, UserNotMutualContactError):
                failed.append(f"{username}: настройки конфиденциальности")
            except UserChannelsTooMuchError:
                failed.append(f"{username}: слишком много каналов")
            except FloodWaitError as e:
                log.warning("invite_users FloodWait %ds", e.seconds)
                await asyncio.sleep(min(e.seconds, 300))
            except Exception as e:
                failed.append(f"{username}: {str(e)[:60]}")
                await asyncio.sleep(random.uniform(5, 15))

        return {"invited": invited, "failed": failed}
    except Exception as e:
        log.exception("invite_users_to_channel outer error: %s", e)
        return {"invited": invited, "failed": failed, "error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_contacts(session_string: str, _acc: dict | None = None) -> list[dict]:
    """Fetch contacts list from a Telegram account.

    Returns list of {user_id, username, phone, first_name, last_name}.
    Bots and deleted accounts are excluded.
    """
    from telethon.tl.functions.contacts import GetContactsRequest
    client = _make_client(session_string, _acc)
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
    session_string: str, channel_id: int, user_id: int, _acc: dict | None = None,
) -> bool:
    """Kick (ban + unban) a user from a channel/group."""
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights
    client = _make_client(session_string, _acc)
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


async def promote_to_admin(
    session_string: str,
    channel_id: int,
    user_id: int,
    _acc: dict | None = None,
    access_hash: int = 0,
    post_messages: bool = True,
    invite_users: bool = True,
    change_info: bool = False,
    delete_messages: bool = False,
    ban_users: bool = False,
    pin_messages: bool = False,
    manage_call: bool = False,
) -> bool:
    """Promote a user to admin in a channel/group.

    Requires calling account to be owner or admin with add_admins right.
    User must already be a member. Returns True on success.
    """
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights, InputPeerChannel, PeerUser
    from telethon.errors import ChatAdminRequiredError, UserNotParticipantError

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve channel with access_hash when available
        if access_hash and isinstance(channel_id, int) and channel_id > 0:
            channel = InputPeerChannel(channel_id=channel_id, access_hash=access_hash)
        else:
            channel = await client.get_entity(channel_id)

        rights = ChatAdminRights(
            post_messages=post_messages,
            edit_messages=False,
            delete_messages=delete_messages,
            ban_users=ban_users,
            invite_users=invite_users,
            pin_messages=pin_messages,
            add_admins=False,
            manage_call=manage_call,
            other=False,
            change_info=change_info,
            anonymous=False,
            manage_topics=False,
        )
        await client(EditAdminRequest(channel=channel, user_id=PeerUser(user_id=user_id),
                                      admin_rights=rights, rank=""))
        log.info("promote_to_admin: user %s promoted in channel %s", user_id, channel_id)
        return True
    except UserNotParticipantError:
        log.warning("promote_to_admin: user %s not yet a member of %s", user_id, channel_id)
        return False
    except ChatAdminRequiredError:
        log.warning("promote_to_admin: calling account lacks add_admins right in %s", channel_id)
        return False
    except Exception as e:
        log.warning("promote_to_admin error user=%s chan=%s: %s", user_id, channel_id, e)
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
    session_string: str, channel_id: int | str, text: str, access_hash: int = 0,
    _acc: dict | None = None,
) -> dict:
    """Post a text message to a channel/group.

    access_hash: if provided, uses InputPeerChannel directly (fast, no cache needed).
    Without access_hash and without @username, fetches dialogs to populate entity cache.

    Returns {"msg_id": int} on success or {"error": str, "flood_wait"?: int} on failure.
    """
    from telethon.tl.types import InputPeerChannel
    from telethon.errors import FloodWaitError, ChatWriteForbiddenError, UserNotParticipantError, UserBannedInChannelError
    client = _make_client(session_string, _acc)
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
    except UserBannedInChannelError as e:
        return {"error": f"Аккаунт забанен в канале: {e}", "banned": True}
    except ChatWriteForbiddenError as e:
        return {"error": f"Нет прав для публикации в этом канале: {e}", "banned": True}
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
    session_string: str, channel_id: int, msg_id: int, emoji: str,
    _acc: dict | None = None,
) -> bool:
    """Send a reaction emoji to a specific message."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    client = _make_client(session_string, _acc)
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
    _acc: dict | None = None,
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
    client = _make_client(session_string, _acc)
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


async def report_peer_deep(
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
    msg_messages: list[str] | None = None,
    max_msg_reports: int = 50,
    block_after: bool = True,
    multi_reason: bool = True,
    join_first: bool = True,
    negative_react: bool = True,
    report_admins: bool = True,
    report_linked_bots: bool = True,
    forward_to_bot: bool = True,
    report_photo: bool = True,
    report_pinned: bool = True,
    report_linked_group: bool = True,
    _acc: dict | None = None,
) -> dict:
    """12-векторная атака на нелегальный ресурс за одно подключение.

    1.  ReportPeer — все доступные причины по кругу (primary + все вторичные)
    2.  ReportProfilePhoto — жалоба на фото профиля канала
    3.  JoinChannel — войти для отчётов изнутри (весят больше)
    4.  Pinned Messages — ReportRequest (закреплённые = приоритет для модераторов)
    5.  Regular Messages — ReportRequest на 50 последних (чанки по 5, все причины)
    6.  channels.ReportSpam — дополнительный спам-сигнал
    7.  Negative Reactions 👎💩 на все доступные посты (до 20)
    8.  Admins — ReportPeer на ВСЕХ администраторов
    9.  Linked Group — ReportPeer на связанную группу обсуждений
    10. Linked Bots — ReportPeer на боты из описания/постов
    11. Forward Evidence → @stopCA / @notoscam
    12. Block + Mute + Leave
    """
    import re as _re

    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.functions.messages import ReportRequest as MsgReportRequest
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import (
        JoinChannelRequest, LeaveChannelRequest, GetParticipantsRequest,
        GetFullChannelRequest,
    )
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence,
        InputReportReasonPornography, InputReportReasonChildAbuse,
        InputReportReasonCopyright, InputReportReasonOther,
        Channel, ChannelParticipantsAdmins, ReactionEmoji,
        InputMessagesFilterPinned,
    )

    # Optional imports — newer TL layers / Telethon versions
    _has_photo_report = False
    try:
        from telethon.tl.functions.account import ReportProfilePhotoRequest
        _has_photo_report = True
    except ImportError:
        pass

    _has_chan_spam = False
    ChanSpamRequest = None
    try:
        from telethon.tl.functions.channels import ReportSpamRequest as _CSR
        ChanSpamRequest = _CSR
        _has_chan_spam = True
    except ImportError:
        pass

    # Build reason map — try to include newer TL types
    reason_map: dict = {
        "spam":        InputReportReasonSpam(),
        "violence":    InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse":  InputReportReasonChildAbuse(),
        "copyright":   InputReportReasonCopyright(),
        "other":       InputReportReasonOther(),
    }
    for _type_name, _key in [
        ("InputReportReasonIllegalDrugs", "drugs"),
        ("InputReportReasonPersonalDetails", "personal"),
        ("InputReportReasonFake", "fake"),
        ("InputReportReasonGeoIrrelevant", "geo"),
    ]:
        try:
            import telethon.tl.types as _tlt
            reason_map[_key] = getattr(_tlt, _type_name)()
        except Exception:
            pass

    # Escalation: primary → all applicable secondary reasons
    _escalation: dict[str, list[str]] = {
        "childabuse":  ["pornography", "violence", "drugs", "spam", "other"],
        "drugs":       ["childabuse", "violence", "spam", "other"],
        "violence":    ["childabuse", "spam", "drugs", "fake", "other"],
        "pornography": ["childabuse", "spam", "other", "violence"],
        "spam":        ["other", "violence", "personal", "fake"],
        "other":       ["spam", "violence", "pornography", "drugs"],
        "copyright":   ["spam", "other"],
    }

    _report_bots: dict[str, str] = {
        "childabuse": "stopCA",
        "drugs":      "stopCA",
        "violence":   "notoscam",
        "other":      "notoscam",
        "spam":       "notoscam",
        "pornography": "notoscam",
    }

    tg_reason = reason_map.get(reason, InputReportReasonOther())
    # Build ordered reason cycle: primary first, then all secondary
    all_reasons_cycle = [tg_reason]
    for sec_key in _escalation.get(reason, []):
        if sec_key in reason_map:
            all_reasons_cycle.append(reason_map[sec_key])

    result = {
        "peer_reported":          False,
        "multi_reason_sent":      0,
        "photo_reported":         False,
        "pinned_reported":        0,
        "msg_reported":           0,
        "spam_signaled":          0,
        "reactions_sent":         0,
        "admins_reported":        0,
        "linked_group_reported":  False,
        "bots_reported":          0,
        "forwarded":              0,
        "blocked":                False,
        "joined":                 False,
    }
    msg_pool = msg_messages or [message] or [""]

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        is_channel = isinstance(entity, Channel)

        # ── 1. ReportPeer — все причины по кругу ──────────────────────────
        for idx, r_obj in enumerate(all_reasons_cycle if multi_reason else [tg_reason]):
            try:
                if idx > 0:
                    await asyncio.sleep(0.35)
                await client(ReportPeerRequest(
                    peer=entity,
                    reason=r_obj,
                    message=msg_pool[idx % len(msg_pool)],
                ))
                if idx == 0:
                    result["peer_reported"] = True
                else:
                    result["multi_reason_sent"] += 1
            except Exception as e:
                log.warning("report_peer_deep[1/peer idx=%d]: %s", idx, e)

        # ── 2. Report Profile Photo ────────────────────────────────────────
        if report_photo and _has_photo_report:
            try:
                from telethon.tl.functions.account import ReportProfilePhotoRequest as _RPP
                photos = await client.get_profile_photos(entity, limit=1)
                if photos:
                    await client(_RPP(
                        peer=entity,
                        photo_id=client._get_input_photo(photos[0]),
                        reason=tg_reason,
                        message=msg_pool[0],
                    ))
                    result["photo_reported"] = True
            except Exception as e:
                log.warning("report_peer_deep[2/photo]: %s", e)

        # ── 3. Вступить в канал для утяжелённых отчётов ───────────────────
        if join_first and is_channel:
            try:
                await client(JoinChannelRequest(entity))
                result["joined"] = True
                await asyncio.sleep(random.uniform(2.0, 4.5))
            except Exception as e:
                log.warning("report_peer_deep[3/join]: %s", e)

        # Get full channel info (linked group, about text)
        full_chat = None
        try:
            full_result = await client(GetFullChannelRequest(entity))
            full_chat = full_result.full_chat
        except Exception:
            pass

        # ── 4. Pinned messages — высший приоритет для модераторов ─────────
        pinned_msgs = []
        if report_pinned and is_channel:
            try:
                pinned_msgs = await client.get_messages(
                    entity, filter=InputMessagesFilterPinned(), limit=20
                )
                pinned_ids = [m.id for m in pinned_msgs if m and m.id]
                for idx_p, pid in enumerate(pinned_ids):
                    r_obj = all_reasons_cycle[idx_p % len(all_reasons_cycle)]
                    try:
                        await client(MsgReportRequest(
                            peer=entity, id=[pid],
                            reason=r_obj,
                            message=msg_pool[idx_p % len(msg_pool)],
                        ))
                        result["pinned_reported"] += 1
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        log.warning("report_peer_deep[4/pinned %d]: %s", pid, e)
            except Exception as e:
                log.warning("report_peer_deep[4/get_pinned]: %s", e)

        # ── 5. Жалобы на последние 50 сообщений (все причины по кругу) ────
        msgs: list = []
        if is_channel:
            try:
                msgs = await client.get_messages(entity, limit=max_msg_reports)
                msg_ids = [m.id for m in msgs if m and m.id]
                chunks = [msg_ids[i:i+5] for i in range(0, len(msg_ids), 5)]
                for chunk_idx, chunk in enumerate(chunks):
                    r_obj = all_reasons_cycle[chunk_idx % len(all_reasons_cycle)]
                    chunk_msg = msg_pool[chunk_idx % len(msg_pool)]
                    try:
                        await client(MsgReportRequest(
                            peer=entity, id=chunk,
                            reason=r_obj, message=chunk_msg,
                        ))
                        result["msg_reported"] += len(chunk)
                    except Exception as e:
                        log.warning("report_peer_deep[5/msg_chunk %d]: %s", chunk_idx, e)
                    await asyncio.sleep(0.55)
            except Exception as e:
                log.warning("report_peer_deep[5/get_msgs]: %s", e)

        # ── 6. channels.ReportSpam (отдельный спам-сигнал) ────────────────
        if _has_chan_spam and ChanSpamRequest and msgs and is_channel:
            spam_ids = [m.id for m in msgs[:10] if m and m.id]
            if spam_ids:
                try:
                    await client(ChanSpamRequest(
                        channel=entity,
                        participant=entity,
                        id=spam_ids,
                    ))
                    result["spam_signaled"] += len(spam_ids)
                except Exception as e:
                    log.warning("report_peer_deep[6/chan_spam]: %s", e)

        # ── 7. Негативные реакции на все доступные посты ──────────────────
        if negative_react and msgs:
            reaction_emojis = ["👎", "💩", "🤮"]
            for r_idx, m in enumerate(msgs[:20]):
                if not (m and m.id):
                    continue
                emoji = reaction_emojis[r_idx % len(reaction_emojis)]
                try:
                    await client(SendReactionRequest(
                        peer=entity,
                        msg_id=m.id,
                        reaction=[ReactionEmoji(emoticon=emoji)],
                    ))
                    result["reactions_sent"] += 1
                    await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning("report_peer_deep[7/react]: %s", e)

        # ── 8. Жалобы на ВСЕХ администраторов ────────────────────────────
        if report_admins and is_channel:
            try:
                admins_result = await client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsAdmins(),
                    offset=0, limit=50, hash=0,
                ))
                admin_users = getattr(admins_result, "users", [])
                for a_idx, usr in enumerate(admin_users):
                    try:
                        await asyncio.sleep(0.4)
                        r_obj = all_reasons_cycle[a_idx % len(all_reasons_cycle)]
                        await client(ReportPeerRequest(
                            peer=usr,
                            reason=r_obj,
                            message=msg_pool[a_idx % len(msg_pool)],
                        ))
                        result["admins_reported"] += 1
                    except Exception as e:
                        log.warning("report_peer_deep[8/admin]: %s", e)
            except Exception as e:
                log.warning("report_peer_deep[8/get_admins]: %s", e)

        # ── 9. Linked discussion group ────────────────────────────────────
        if report_linked_group and full_chat:
            linked_id = getattr(full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    linked_entity = await client.get_entity(int(linked_id))
                    for idx_lg, r_obj in enumerate(all_reasons_cycle[:3]):
                        try:
                            await asyncio.sleep(0.5)
                            await client(ReportPeerRequest(
                                peer=linked_entity,
                                reason=r_obj,
                                message=msg_pool[idx_lg % len(msg_pool)],
                            ))
                            result["linked_group_reported"] = True
                        except Exception as e:
                            log.warning("report_peer_deep[9/linked reason %d]: %s", idx_lg, e)
                except Exception as e:
                    log.warning("report_peer_deep[9/get_linked]: %s", e)

        # ── 10. Linked bots → ReportPeer ──────────────────────────────────
        if report_linked_bots and is_channel:
            bot_re = _re.compile(r'@([A-Za-z]\w{4,31}[Bb]ot)\b')
            scan_text = ""
            if full_chat:
                scan_text += (getattr(full_chat, "about", "") or "") + " "
            for m in msgs[:5]:
                if m and m.text:
                    scan_text += m.text + " "
            found_bots = list(set(bot_re.findall(scan_text)))[:5]
            for b_idx, bot_uname in enumerate(found_bots):
                try:
                    bot_entity = await client.get_entity(bot_uname)
                    r_obj = all_reasons_cycle[b_idx % len(all_reasons_cycle)]
                    await client(ReportPeerRequest(
                        peer=bot_entity, reason=r_obj,
                        message=msg_pool[b_idx % len(msg_pool)],
                    ))
                    result["bots_reported"] += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning("report_peer_deep[10/bot %s]: %s", bot_uname, e)

        # ── 11. Forward evidence → @stopCA / @notoscam ─────────────────────
        if forward_to_bot and msgs:
            bot_username = _report_bots.get(reason, "notoscam")
            try:
                bot_ent = await client.get_entity(bot_username)
                evidence_msgs = [m for m in msgs[:5] if m and not m.service]
                for em in evidence_msgs:
                    try:
                        await client.forward_messages(bot_ent, em)
                        result["forwarded"] += 1
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        log.warning("report_peer_deep[11/fwd]: %s", e)
            except Exception as e:
                log.warning("report_peer_deep[11/get_bot]: %s", e)

        # ── 12. Mute + Block + Leave ───────────────────────────────────────
        try:
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=entity),
                settings=InputPeerNotifySettings(mute_until=2_147_483_647),
            ))
        except Exception:
            pass

        if result["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception:
                pass
        if block_after:
            try:
                await client(BlockRequest(id=entity))
                result["blocked"] = True
            except Exception as e:
                log.warning("report_peer_deep[12/block]: %s", e)

    except Exception as e:
        log.exception("report_peer_deep error: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


async def report_peer_deep_v2(
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
    msg_messages: list[str] | None = None,
    max_msg_reports: int = 60,
    block_after: bool = True,
    multi_reason: bool = True,
    join_first: bool = True,
    negative_react: bool = True,
    report_admins: bool = True,
    report_linked_bots: bool = True,
    forward_to_bot: bool = True,
    report_photo: bool = True,
    report_pinned: bool = True,
    report_linked_group: bool = True,
    wave_num: int = 0,
    _acc: dict | None = None,
) -> dict:
    """12-векторная атака v2 — улучшенная версия с адаптивным таймингом.

    Отличия от v1:
      - Human-like задержки через session_simulator (не фиксированные)
      - FloodWait-устойчивость: перехват и backoff внутри функции
      - Волновая логика: join_first только в wave 0, block_after в последней
      - Больше сообщений: max_msg_reports до 60 (было 50)
      - Лучшая обработка ошибок: каждый вектор изолирован
      - Увеличен пул причин: geo, fake, personal + базовые
      - Ретраи при временных ошибках соединения
    """
    import re as _re
    from services import session_simulator

    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.functions.messages import ReportRequest as MsgReportRequest
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import (
        JoinChannelRequest, LeaveChannelRequest, GetParticipantsRequest,
        GetFullChannelRequest,
    )
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence,
        InputReportReasonPornography, InputReportReasonChildAbuse,
        InputReportReasonCopyright, InputReportReasonOther,
        Channel, ChannelParticipantsAdmins, ReactionEmoji,
        InputMessagesFilterPinned,
    )

    # Optional imports
    _has_photo_report = False
    try:
        from telethon.tl.functions.account import ReportProfilePhotoRequest
        _has_photo_report = True
    except ImportError:
        pass

    _has_chan_spam = False
    ChanSpamRequest = None
    try:
        from telethon.tl.functions.channels import ReportSpamRequest as _CSR
        ChanSpamRequest = _CSR
        _has_chan_spam = True
    except ImportError:
        pass

    # Build reason map
    reason_map: dict = {
        "spam":        InputReportReasonSpam(),
        "violence":    InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse":  InputReportReasonChildAbuse(),
        "copyright":   InputReportReasonCopyright(),
        "other":       InputReportReasonOther(),
    }
    for _type_name, _key in [
        ("InputReportReasonIllegalDrugs", "drugs"),
        ("InputReportReasonPersonalDetails", "personal"),
        ("InputReportReasonFake", "fake"),
        ("InputReportReasonGeoIrrelevant", "geo"),
    ]:
        try:
            import telethon.tl.types as _tlt
            reason_map[_key] = getattr(_tlt, _type_name)()
        except Exception:
            pass

    _escalation: dict[str, list[str]] = {
        "childabuse":  ["pornography", "violence", "drugs", "spam", "other"],
        "csam":        ["pornography", "violence", "drugs", "spam", "other"],
        "drugs":       ["childabuse", "violence", "spam", "other"],
        "violence":    ["childabuse", "spam", "drugs", "fake", "other"],
        "terrorism":   ["childabuse", "violence", "spam", "drugs", "other"],
        "pornography": ["childabuse", "spam", "other", "violence"],
        "spam":        ["other", "violence", "personal", "fake"],
        "other":       ["spam", "violence", "pornography", "drugs"],
        "copyright":   ["spam", "other"],
        "fraud":       ["spam", "other", "fake", "violence"],
        "weapons":     ["violence", "spam", "other"],
        "darknet":     ["spam", "other", "drugs"],
    }

    _report_bots: dict[str, str] = {
        "childabuse": "stopCA",
        "csam":       "stopCA",
        "drugs":      "stopCA",
        "violence":   "notoscam",
        "other":      "notoscam",
        "spam":       "notoscam",
        "pornography": "notoscam",
        "fraud":      "notoscam",
    }

    tg_reason = reason_map.get(reason, InputReportReasonOther())
    all_reasons_cycle = [tg_reason]
    for sec_key in _escalation.get(reason, []):
        if sec_key in reason_map:
            all_reasons_cycle.append(reason_map[sec_key])

    result = {
        "peer_reported":          False,
        "multi_reason_sent":      0,
        "photo_reported":         False,
        "pinned_reported":        0,
        "msg_reported":           0,
        "spam_signaled":          0,
        "reactions_sent":         0,
        "admins_reported":        0,
        "linked_group_reported":  False,
        "bots_reported":          0,
        "forwarded":              0,
        "blocked":                False,
        "joined":                 False,
    }
    msg_pool = msg_messages or [message] or [""]

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        is_channel = isinstance(entity, Channel)

        # ── 1. ReportPeer — все причины по кругу с human-like задержками ────
        for idx, r_obj in enumerate(all_reasons_cycle if multi_reason else [tg_reason]):
            try:
                if idx > 0:
                    # Human-like задержка между жалобами
                    delay = random.betavariate(2, 5) * 2.0 + 0.3
                    await asyncio.sleep(delay)
                # Pre-action hesitation (12% вероятность)
                if random.random() < 0.12:
                    await asyncio.sleep(random.uniform(1.0, 4.0))
                await client(ReportPeerRequest(
                    peer=entity,
                    reason=r_obj,
                    message=msg_pool[idx % len(msg_pool)],
                ))
                if idx == 0:
                    result["peer_reported"] = True
                else:
                    result["multi_reason_sent"] += 1
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err.upper():
                    wait_s = _extract_flood_wait(err, 30)
                    await asyncio.sleep(wait_s + random.uniform(1, 5))
                    # Ретрай после FloodWait
                    try:
                        await client(ReportPeerRequest(
                            peer=entity, reason=r_obj,
                            message=msg_pool[idx % len(msg_pool)],
                        ))
                        if idx == 0:
                            result["peer_reported"] = True
                        else:
                            result["multi_reason_sent"] += 1
                    except Exception:
                        pass
                else:
                    log.warning("rpv2[1/peer idx=%d]: %s", idx, e)

        await session_simulator.short_pause(0.5, 1.5)

        # ── 2. Report Profile Photo ────────────────────────────────────────
        if report_photo and _has_photo_report:
            try:
                from telethon.tl.functions.account import ReportProfilePhotoRequest as _RPP
                photos = await client.get_profile_photos(entity, limit=1)
                if photos:
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                    await client(_RPP(
                        peer=entity,
                        photo_id=client._get_input_photo(photos[0]),
                        reason=tg_reason,
                        message=msg_pool[0],
                    ))
                    result["photo_reported"] = True
            except Exception as e:
                log.warning("rpv2[2/photo]: %s", e)

        # ── 3. Join channel (только для первой волны) ──────────────────────
        if join_first and is_channel and wave_num == 0:
            try:
                await session_simulator.action_hesitation(0.15)
                await client(JoinChannelRequest(entity))
                result["joined"] = True
                # Человеческая пауза после входа — "читает канал"
                await asyncio.sleep(random.uniform(3.0, 7.0))
            except Exception as e:
                log.warning("rpv2[3/join]: %s", e)

        # Get full channel info
        full_chat = None
        try:
            full_result = await client(GetFullChannelRequest(entity))
            full_chat = full_result.full_chat
        except Exception:
            pass

        # ── 4. Pinned messages — приоритет для модераторов ─────────────────
        pinned_msgs = []
        if report_pinned and is_channel:
            try:
                pinned_msgs = await client.get_messages(
                    entity, filter=InputMessagesFilterPinned(), limit=25
                )
                pinned_ids = [m.id for m in pinned_msgs if m and m.id]
                for idx_p, pid in enumerate(pinned_ids):
                    r_obj = all_reasons_cycle[idx_p % len(all_reasons_cycle)]
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 1.5 + 0.3)
                        await client(MsgReportRequest(
                            peer=entity, id=[pid],
                            reason=r_obj,
                            message=msg_pool[idx_p % len(msg_pool)],
                        ))
                        result["pinned_reported"] += 1
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_extract_flood_wait(err, 15))
                        else:
                            log.warning("rpv2[4/pinned %d]: %s", pid, e)
            except Exception as e:
                log.warning("rpv2[4/get_pinned]: %s", e)

        # ── 5. Recent messages (чанки по 5, до max_msg_reports) ────────────
        msgs: list = []
        if is_channel:
            try:
                msgs = await client.get_messages(entity, limit=max_msg_reports)
                msg_ids = [m.id for m in msgs if m and m.id]
                # Shuffle chunks for variety
                chunks = [msg_ids[i:i+5] for i in range(0, len(msg_ids), 5)]
                random.shuffle(chunks)
                for chunk_idx, chunk in enumerate(chunks):
                    r_obj = all_reasons_cycle[chunk_idx % len(all_reasons_cycle)]
                    chunk_msg = msg_pool[chunk_idx % len(msg_pool)]
                    try:
                        await client(MsgReportRequest(
                            peer=entity, id=chunk,
                            reason=r_obj, message=chunk_msg,
                        ))
                        result["msg_reported"] += len(chunk)
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_extract_flood_wait(err, 15))
                            # Ретрай с меньшим чанком
                            try:
                                await client(MsgReportRequest(
                                    peer=entity, id=chunk[:2],
                                    reason=r_obj, message=chunk_msg,
                                ))
                                result["msg_reported"] += min(2, len(chunk))
                            except Exception:
                                pass
                        else:
                            log.warning("rpv2[5/msg_chunk %d]: %s", chunk_idx, e)
                    # Human-like пауза между чанками
                    await asyncio.sleep(random.betavariate(2, 5) * 1.2 + 0.3)
            except Exception as e:
                log.warning("rpv2[5/get_msgs]: %s", e)

        # ── 6. channels.ReportSpam ─────────────────────────────────────────
        if _has_chan_spam and ChanSpamRequest and msgs and is_channel:
            spam_ids = [m.id for m in msgs[:15] if m and m.id]
            if spam_ids:
                try:
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                    await client(ChanSpamRequest(
                        channel=entity,
                        participant=entity,
                        id=spam_ids,
                    ))
                    result["spam_signaled"] += len(spam_ids)
                except Exception as e:
                    log.warning("rpv2[6/chan_spam]: %s", e)

        # ── 7. Negative reactions (разнообразные эмодзи) ────────────────────
        if negative_react and msgs:
            reaction_sets = [
                ["👎", "💩", "🤮"],
                ["👎", "🤬", "💩"],
                ["👎", "🤮"],
                ["💩", "🤬"],
                ["👎"],
            ]
            reactions_pool = reaction_sets[wave_num % len(reaction_sets)]
            for r_idx, m in enumerate(msgs[:20]):
                if not (m and m.id):
                    continue
                emoji = reactions_pool[r_idx % len(reactions_pool)]
                try:
                    await client(SendReactionRequest(
                        peer=entity,
                        msg_id=m.id,
                        reaction=[ReactionEmoji(emoticon=emoji)],
                    ))
                    result["reactions_sent"] += 1
                    await asyncio.sleep(random.betavariate(2, 4) * 0.8 + 0.15)
                except Exception as e:
                    log.warning("rpv2[7/react]: %s", e)

        # ── 8. Report admins ───────────────────────────────────────────────
        if report_admins and is_channel:
            try:
                admins_result = await client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsAdmins(),
                    offset=0, limit=50, hash=0,
                ))
                admin_users = getattr(admins_result, "users", [])
                # Shuffle admins to vary report order
                random.shuffle(admin_users)
                for a_idx, usr in enumerate(admin_users):
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 1.0 + 0.3)
                        r_obj = all_reasons_cycle[a_idx % len(all_reasons_cycle)]
                        await client(ReportPeerRequest(
                            peer=usr,
                            reason=r_obj,
                            message=msg_pool[a_idx % len(msg_pool)],
                        ))
                        result["admins_reported"] += 1
                    except Exception as e:
                        log.warning("rpv2[8/admin]: %s", e)
            except Exception as e:
                log.warning("rpv2[8/get_admins]: %s", e)

        # ── 9. Linked discussion group ─────────────────────────────────────
        if report_linked_group and full_chat:
            linked_id = getattr(full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    linked_entity = await client.get_entity(int(linked_id))
                    for idx_lg in range(min(4, len(all_reasons_cycle))):
                        try:
                            await asyncio.sleep(random.betavariate(2, 5) * 1.2 + 0.4)
                            r_obj = all_reasons_cycle[idx_lg]
                            await client(ReportPeerRequest(
                                peer=linked_entity,
                                reason=r_obj,
                                message=msg_pool[idx_lg % len(msg_pool)],
                            ))
                            result["linked_group_reported"] = True
                        except Exception as e:
                            log.warning("rpv2[9/linked reason %d]: %s", idx_lg, e)
                except Exception as e:
                    log.warning("rpv2[9/get_linked]: %s", e)

        # ── 10. Linked bots ────────────────────────────────────────────────
        if report_linked_bots and is_channel:
            bot_re = _re.compile(r'@([A-Za-z]\w{4,31}[Bb]ot)\b')
            scan_text = ""
            if full_chat:
                scan_text += (getattr(full_chat, "about", "") or "") + " "
            # Scan more messages for bots
            for m in (msgs or [])[:10]:
                if m and m.text:
                    scan_text += m.text + " "
            found_bots = list(set(bot_re.findall(scan_text)))[:6]
            for b_idx, bot_uname in enumerate(found_bots):
                try:
                    bot_entity = await client.get_entity(bot_uname)
                    r_obj = all_reasons_cycle[b_idx % len(all_reasons_cycle)]
                    await client(ReportPeerRequest(
                        peer=bot_entity, reason=r_obj,
                        message=msg_pool[b_idx % len(msg_pool)],
                    ))
                    result["bots_reported"] += 1
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                except Exception as e:
                    log.warning("rpv2[10/bot %s]: %s", bot_uname, e)

        # ── 11. Forward evidence to @stopCA / @notoscam ─────────────────────
        if forward_to_bot and msgs:
            bot_username = _report_bots.get(reason, "notoscam")
            try:
                bot_ent = await client.get_entity(bot_username)
                evidence_msgs = [m for m in msgs[:8] if m and not m.service]
                for em in evidence_msgs:
                    try:
                        await client.forward_messages(bot_ent, em)
                        result["forwarded"] += 1
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                    except Exception as e:
                        log.warning("rpv2[11/fwd]: %s", e)
            except Exception as e:
                log.warning("rpv2[11/get_bot]: %s", e)

        # ── 12. Mute + Block + Leave ───────────────────────────────────────
        try:
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=entity),
                settings=InputPeerNotifySettings(mute_until=2_147_483_647),
            ))
        except Exception:
            pass

        if result["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception:
                pass
        if block_after:
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await client(BlockRequest(id=entity))
                result["blocked"] = True
            except Exception as e:
                log.warning("rpv2[12/block]: %s", e)

    except Exception as e:
        log.exception("report_peer_deep_v2 error: %s", e)
        # Добавляем информацию об ошибке в результат
        result["_fatal_error"] = str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return result


def _extract_flood_wait(err_str: str, default: float = 30.0) -> float:
    """Извлекает секунды ожидания из ошибки FloodWait."""
    import re as _re
    match = _re.search(r'(\d+)', err_str)
    if match:
        return min(_FLOOD_CAP, float(match.group(1)))
    return default


async def strike_map_target(
    session_string: str,
    peer_username: str,
    _acc: dict | None = None,
) -> dict:
    """Разведка перед атакой: полная карта цели за одно подключение.

    Возвращает:
      channel_id, title, description, members, access_hash,
      admin_ids[], linked_group_id, pinned_msg_ids[], latest_msg_ids[],
      mentioned_usernames[], bot_usernames[], error
    """
    import re as _re
    from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest
    from telethon.tl.functions.messages import GetPinnedMessagesRequest
    from telethon.tl.types import Channel, ChannelParticipantsAdmins, InputMessagesFilterPinned

    intel: dict = {
        "channel_id": 0, "title": "", "description": "", "members": 0,
        "access_hash": 0, "admin_ids": [], "linked_group_id": None,
        "pinned_msg_ids": [], "latest_msg_ids": [],
        "mentioned_usernames": [], "bot_usernames": [], "error": None,
    }

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        if not isinstance(entity, Channel):
            intel["error"] = "not_a_channel"
            return intel

        intel["channel_id"]  = entity.id
        intel["title"]       = getattr(entity, "title", "") or ""
        intel["access_hash"] = getattr(entity, "access_hash", 0) or 0
        intel["members"]     = getattr(entity, "participants_count", 0) or 0

        # Полная инфо о канале (описание, linked_chat_id)
        try:
            full = await client(GetFullChannelRequest(entity))
            fc = full.full_chat
            intel["description"]    = (getattr(fc, "about", "") or "")[:500]
            intel["linked_group_id"] = getattr(fc, "linked_chat_id", None)
        except Exception:
            pass

        # Все администраторы (до 200)
        try:
            adm = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsAdmins(),
                offset=0, limit=200, hash=0,
            ))
            intel["admin_ids"] = [u.id for u in getattr(adm, "users", [])]
        except Exception:
            pass

        # Закреплённые сообщения
        try:
            pinned = await client.get_messages(entity, filter=InputMessagesFilterPinned(), limit=20)
            intel["pinned_msg_ids"] = [m.id for m in pinned if m and m.id]
        except Exception:
            pass

        # Последние 100 сообщений
        try:
            msgs = await client.get_messages(entity, limit=100)
            intel["latest_msg_ids"] = [m.id for m in msgs if m and m.id]
        except Exception:
            pass

        # Упомянутые @usernames и @botы из описания + последних постов
        scan_text = intel["description"]
        try:
            msgs_text = await client.get_messages(entity, limit=15)
            for m in msgs_text:
                if m and m.text:
                    scan_text += " " + m.text
        except Exception:
            pass
        _bot_re    = _re.compile(r'@([A-Za-z]\w{3,31}[Bb]ot)\b')
        _chan_re   = _re.compile(r't\.me/([A-Za-z][A-Za-z0-9_]{3,31})\b')
        _at_re    = _re.compile(r'@([A-Za-z][A-Za-z0-9_]{3,31})\b')
        intel["bot_usernames"]        = list(set(_bot_re.findall(scan_text)))[:8]
        intel["mentioned_usernames"]  = list({
            m for m in _at_re.findall(scan_text)
            if m.lower() not in {"stopca", "notoscam", "spambot"}
        })[:10]
        # t.me/... ссылки
        intel["mentioned_usernames"] += [
            u for u in _chan_re.findall(scan_text)
            if u not in intel["mentioned_usernames"]
        ][:5]

    except Exception as e:
        intel["error"] = str(e)[:200]
        log.warning("strike_map_target error for %s: %s", peer_username, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return intel


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT PROFILE
# ══════════════════════════════════════════════════════════════════════════════

async def update_profile(
    session_string: str,
    first_name: str | None = None,
    last_name: str | None = None,
    about: str | None = None,
    _acc: dict | None = None,
) -> bool:
    """Update the connected account's profile. Pass None to leave a field unchanged."""
    from telethon.tl.functions.account import UpdateProfileRequest
    client = _make_client(session_string, _acc)
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
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            raise
        log.exception("update_profile error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def update_account_username(session_string: str, username: str,
                                  _acc: dict | None = None) -> str:
    """Update account username. Returns '' on success, error string on failure."""
    from telethon.tl.functions.account import UpdateUsernameRequest
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client(UpdateUsernameRequest(username=username.lstrip("@")))
        return ""
    except Exception as e:
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            return f"FloodWait {e.seconds}с — подождите перед изменением username"
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
    _acc: dict | None = None,
) -> dict:
    """Create a new Telegram bot via @BotFather automated dialog.

    Returns dict with 'token' and 'username' on success,
    or 'error' key with message on failure.
    """
    import re
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        def _parse_flood_wait(text: str) -> int | None:
            """Extract wait seconds from BotFather 'too many attempts' message."""
            m = re.search(r"try again in (\d+) seconds", text, re.IGNORECASE)
            return int(m.group(1)) if m else None

        async def _bf_send(text: str) -> str:
            """Send message to BotFather with human-like delay, return its response."""
            # Random pre-send pause — like a human thinking before typing
            await asyncio.sleep(random.uniform(2.0, 5.0))
            await client.send_message(_BOTFATHER_USERNAME, text)
            # Random wait for BotFather to respond
            await asyncio.sleep(random.uniform(5.0, 9.0))
            msgs = await client.get_messages(_BOTFATHER_USERNAME, limit=1)
            return msgs[0].text if msgs else ""

        async def _bf_send_with_retry(text: str, max_retries: int = 2) -> str:
            """Send to BotFather, handling rate limit gracefully."""
            for attempt in range(max_retries + 1):
                resp = await _bf_send(text)
                wait = _parse_flood_wait(resp)
                if wait is None:
                    return resp
                # Rate limited — wait exactly as BotFather says + random buffer
                jitter = random.randint(10, 30)
                total_wait = wait + jitter
                log.info("BotFather rate limit: waiting %ds (asked %ds + %ds buffer)", total_wait, wait, jitter)
                if attempt == max_retries:
                    return resp  # Return the error response to caller
                await asyncio.sleep(total_wait)
            return ""

        # Step 1: start fresh — detect if in a previous incomplete flow
        resp = await _bf_send_with_retry("/newbot")

        # Check for rate limit in initial response
        wait = _parse_flood_wait(resp)
        if wait is not None:
            return {"error": f"BotFather: слишком много попыток, подождите {wait}с", "flood_wait": wait}

        if "name" not in resp.lower() and "alright" not in resp.lower() and "good name" not in resp.lower():
            # Previous incomplete flow — cancel it, then retry once
            await _bf_send("/cancel")
            await asyncio.sleep(random.uniform(3.0, 6.0))
            resp = await _bf_send_with_retry("/newbot")
            wait = _parse_flood_wait(resp)
            if wait is not None:
                return {"error": f"BotFather: слишком много попыток, подождите {wait}с", "flood_wait": wait}

        if "name" not in resp.lower() and "alright" not in resp.lower():
            return {"error": f"Неожиданный ответ BotFather: {resp[:200]}"}

        # Step 2: send display name
        resp = await _bf_send_with_retry(bot_display_name)
        if "username" not in resp.lower():
            wait = _parse_flood_wait(resp)
            if wait is not None:
                return {"error": f"BotFather rate limit после имени: {wait}с", "flood_wait": wait}
            return {"error": f"Неожиданный ответ после имени бота: {resp[:200]}"}

        # Step 3: send username
        uname = bot_username.lstrip("@")
        if not uname.lower().endswith("bot"):
            uname = uname + "bot"
        resp = await _bf_send_with_retry(uname)

        # Extract token (format: 123456789:AAABBBCCC...)
        token_match = re.search(r"\b(\d{8,12}:[A-Za-z0-9_-]{35,})\b", resp)
        if not token_match:
            wait = _parse_flood_wait(resp)
            if wait is not None:
                return {"error": f"BotFather rate limit при создании: {wait}с", "flood_wait": wait}
            if "username" in resp.lower() and ("already" in resp.lower() or "taken" in resp.lower()):
                return {"error": f"Username @{uname} уже занят — выберите другой"}
            return {"error": f"Токен не найден в ответе BotFather: {resp[:300]}"}

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
