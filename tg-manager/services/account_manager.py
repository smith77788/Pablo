"""Telethon user account session management."""
from __future__ import annotations
import asyncio
import logging
import random
from config import TG_API_ID, TG_API_HASH, TG_PROXY

from services.logger import log_exc_swallow

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
            log_exc_swallow(log, "Сбой в start_login")
        raise
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в start_login")
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
            log_exc_swallow(log, "Сбой в import_from_session_string")

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

async def import_from_session_file(session_bytes: bytes, filename: str = "") -> tuple[str, dict]:
    """Convert a Telethon .session SQLite file to StringSession.

    The .session file is a SQLite database with a 'sessions' table:
    dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB
    """
    import sqlite3
    import struct
    import base64
    import tempfile
    import os
    from ipaddress import IPv4Address
    from telethon.sessions import StringSession

    # Write bytes to temp file for sqlite3 to open
    tmp = tempfile.NamedTemporaryFile(suffix=".session", delete=False)
    try:
        tmp.write(session_bytes)
        tmp.flush()
        tmp.close()

        try:
            conn = sqlite3.connect(tmp.name)
            cur = conn.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"Файл не является корректным .session файлом: {e}")

        if not row:
            raise ValueError("Таблица sessions пустая — сессия не авторизована.")

        dc_id, server_address, port, auth_key_bytes = row
        if not auth_key_bytes or len(auth_key_bytes) != 256:
            raise ValueError(
                f"Некорректный auth_key в сессии (длина: {len(auth_key_bytes) if auth_key_bytes else 0}, нужно 256)."
            )

        # Build StringSession in Telethon format (version 1)
        try:
            ip_bytes = IPv4Address(server_address).packed
        except Exception:
            DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51",
                      3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
            ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed

        packed = struct.pack(">B4sH256s", dc_id, ip_bytes, int(port or 443), bytes(auth_key_bytes))
        session_string = "1" + base64.urlsafe_b64encode(packed).decode()

    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    return await import_from_session_string(session_string)

async def convert_session_file_to_string(session_bytes: bytes) -> str:
    """Convert a Telethon .session SQLite file bytes to a StringSession string.

    Unlike import_from_session_file, this does NOT connect to Telegram.
    Returns the raw StringSession string for use in batch import.
    Raises ValueError on invalid file.
    """
    import sqlite3
    import struct
    import base64
    import tempfile
    import os
    from ipaddress import IPv4Address

    tmp = tempfile.NamedTemporaryFile(suffix=".session", delete=False)
    try:
        tmp.write(session_bytes)
        tmp.flush()
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            cur = conn.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"Не является .session файлом: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    if not row:
        raise ValueError("Таблица sessions пустая — сессия не авторизована")
    dc_id, server_address, port, auth_key_bytes = row
    if not auth_key_bytes or len(auth_key_bytes) != 256:
        raise ValueError(f"Некорректный auth_key (длина: {len(auth_key_bytes) if auth_key_bytes else 0})")
    try:
        ip_bytes = IPv4Address(server_address).packed
    except Exception:
        DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51",
                  3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
        ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed
    packed = struct.pack(">B4sH256s", dc_id, ip_bytes, int(port or 443), bytes(auth_key_bytes))
    return "1" + base64.urlsafe_b64encode(packed).decode()

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
            log_exc_swallow(log, "Сбой в import_from_tdata")

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
            log_exc_swallow(log, "Сбой в cleanup_pending")

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
            log_exc_swallow(log, "Сбой в cleanup_qr_pending")

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
            log_exc_swallow(log, "Сбой в get_account_info")

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
                    "type": (
                        "channel" if isinstance(entity, Channel) and getattr(entity, "broadcast", False)
                        else "megagroup" if isinstance(entity, Channel) and getattr(entity, "megagroup", False)
                        else "supergroup" if isinstance(entity, Channel)
                        else "group"
                    ),
                    "members": getattr(entity, "participants_count", 0) or 0,
                    "username": getattr(entity, "username", "") or "",
                    "access_hash": getattr(entity, "access_hash", 0) or 0,
                })
        return dialogs
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_dialogs")

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
            log_exc_swallow(log, "Сбой в _collect")

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
            log_exc_swallow(log, "Сбой в send_message_via_account")
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
            log_exc_swallow(log, "Сбой в send_dm")

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
            log_exc_swallow(log, "Сбой в get_account_dialogs_stats")

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
            log_exc_swallow(log, "Таймаут при проверке статуса через @SpamBot — считаем аккаунт активным")
        except Exception:
            log_exc_swallow(log, "Сбой в check_account_status_full")
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
            log_exc_swallow(log, "Сбой в get_channel_members_count")

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
            log_exc_swallow(log, "Сбой в get_full_channel_info")

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
            log_exc_swallow(log, "Сбой в get_recent_messages")

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
            log_exc_swallow(log, "Сбой в create_channel")
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
            log_exc_swallow(log, "Сбой в create_channel")

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
            log_exc_swallow(log, "Сбой в join_channel")

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
            log_exc_swallow(log, "Сбой в leave_channel")

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
            log_exc_swallow(log, "Сбой в edit_channel_title")

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
            log_exc_swallow(log, "Сбой в edit_channel_about")

async def set_channel_username(
    session_string: str, channel_id: int, username: str, _acc: dict | None = None,
) -> str:
    """Set public username for channel. Returns '' on success, error string on failure."""
    from telethon.tl.functions.channels import UpdateUsernameRequest
    from telethon.tl.types import PeerChannel
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(PeerChannel(channel_id))
        await client(UpdateUsernameRequest(channel=entity, username=username.lstrip("@")))
        return ""
    except Exception as e:
        log.exception("set_channel_username error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в set_channel_username")

def _normalize_channel_id(channel_ref: int | str) -> int:
    cid = abs(int(channel_ref))
    raw = str(cid)
    if raw.startswith("100") and len(raw) > 10:
        return int(raw[3:])
    return cid


async def _resolve_channel_peer(client, channel_ref: int | str, access_hash: int = 0):
    from telethon.tl.types import InputPeerChannel

    if access_hash and isinstance(channel_ref, int) and channel_ref > 0:
        return InputPeerChannel(channel_id=channel_ref, access_hash=access_hash)

    if isinstance(channel_ref, str) and not channel_ref.lstrip("-").isdigit():
        return await client.get_entity(channel_ref)

    target_id = _normalize_channel_id(channel_ref)
    try:
        return await client.get_entity(target_id)
    except Exception:
        async for dlg in client.iter_dialogs(limit=500):
            eid = getattr(dlg.entity, "id", None)
            if eid and abs(int(eid)) == target_id:
                ah = getattr(dlg.entity, "access_hash", 0)
                if ah:
                    return InputPeerChannel(channel_id=target_id, access_hash=ah)
                return dlg.entity
    raise ValueError(f"Channel {channel_ref} not found in account dialogs")


async def get_channel_invite_link(session_string: str, channel_id: int | str,
                                  _acc: dict | None = None, access_hash: int = 0) -> str:
    """Get (or create) an invite link for the channel. Returns link string or ''."""
    from telethon.tl.functions.messages import ExportChatInviteRequest
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await _resolve_channel_peer(client, channel_id, access_hash)
        result = await client(ExportChatInviteRequest(peer=entity))
        return getattr(result, "link", "") or ""
    except Exception as e:
        log.exception("get_channel_invite_link error: %s", e)
        return ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_channel_invite_link")

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
            log_exc_swallow(log, "Сбой в delete_channel")

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
            log_exc_swallow(log, "Сбой в get_channel_members")

async def invite_users_to_channel(
    session_string: str,
    channel_id: int | str,
    usernames: list[str],
    _acc: dict | None = None,
    access_hash: int = 0,
    batch_size: int = 100,
    batch_delay: float = 60.0,
    progress_cb=None,
) -> dict:
    """Invite users to a channel with batching (max batch_size per round).

    Telegram hard limit: ~200 invites/day per account per channel.
    batch_size <= 100 is safe; batch_delay is pause between batches (seconds).
    progress_cb(done, total, invited, failed_count) — optional async callback.
    Returns {invited: int, failed: list[str], batches: int, error?: str}.
    """
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.errors import (
        FloodWaitError, PeerFloodError, UserBannedInChannelError,
        ChatAdminRequiredError, UserPrivacyRestrictedError,
        UserNotMutualContactError, UserChannelsTooMuchError,
    )
    from services import session_simulator

    # Clamp batch_size to Telegram safe limit
    batch_size = max(1, min(batch_size, 200))
    invited = 0
    failed: list[str] = []
    batches_done = 0
    client = _make_client(session_string, _acc)

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve channel entity
        try:
            channel_peer = await _resolve_channel_peer(client, channel_id, access_hash)
        except Exception:
            return {"invited": 0, "failed": [], "batches": 0,
                            "error": f"Канал {channel_id} не найден в диалогах аккаунта"}

        # Split into batches of batch_size
        batches = [usernames[i:i + batch_size] for i in range(0, len(usernames), batch_size)]
        total = len(usernames)
        done = 0
        abort = False

        for b_idx, batch in enumerate(batches):
            if abort:
                for u in batch:
                    failed.append(f"{u.strip()}: пропущен (аккаунт ограничен)")
                continue

            if b_idx > 0:
                log.info("invite batch %d/%d: cooldown %.0fs", b_idx + 1, len(batches), batch_delay)
                await asyncio.sleep(batch_delay)

            for idx, username in enumerate(batch):
                uname = username.strip()
                try:
                    user = await asyncio.wait_for(client.get_entity(uname), timeout=10.0)
                    await client(InviteToChannelRequest(channel=channel_peer, users=[user]))
                    invited += 1
                    done += 1
                    if progress_cb and done % 10 == 0:
                        try:
                            await progress_cb(done, total, invited, len(failed))
                        except Exception:
                            pass
                    if idx < len(batch) - 1:
                        await asyncio.sleep(random.uniform(35, 95) * session_simulator.chaos_factor())
                except ChatAdminRequiredError:
                    for u in batch[idx + 1:] + [u2 for b2 in batches[b_idx + 1:] for u2 in b2]:
                        failed.append(f"{u.strip()}: нет прав администратора")
                    abort = True
                    return {"invited": invited, "failed": failed, "batches": batches_done,
                            "error": "Нет прав администратора. Назначьте аккаунт администратором с правом 'Добавление участников'."}
                except PeerFloodError:
                    for u in batch[idx + 1:]:
                        failed.append(f"{u.strip()}: PeerFlood")
                    abort = True
                    return {"invited": invited, "failed": failed, "batches": batches_done,
                            "error": "PeerFlood: account stopped to avoid spamblock escalation"}
                except UserBannedInChannelError:
                    failed.append(f"{uname}: забанен в канале")
                except (UserPrivacyRestrictedError, UserNotMutualContactError):
                    failed.append(f"{uname}: настройки конфиденциальности")
                except UserChannelsTooMuchError:
                    failed.append(f"{uname}: слишком много каналов")
                except FloodWaitError as e:
                    wait_s = min(int(e.seconds), 600)
                    log.warning("invite FloodWait %ds acc=%s", wait_s, (_acc or {}).get("id", "?"))
                    await asyncio.sleep(wait_s + random.uniform(5, 15))
                    # Retry once after flood
                    try:
                        user = await asyncio.wait_for(client.get_entity(uname), timeout=10.0)
                        await client(InviteToChannelRequest(channel=channel_peer, users=[user]))
                        invited += 1
                    except Exception:
                        failed.append(f"{uname}: FloodWait+retry_fail")
                except Exception as e:
                    failed.append(f"{uname}: {str(e)[:60]}")
                    await asyncio.sleep(random.uniform(3, 8))

            batches_done += 1
            log.info("invite batch %d/%d done: +%d invited, %d failed total",
                     b_idx + 1, len(batches), invited, len(failed))

        if progress_cb:
            try:
                await progress_cb(total, total, invited, len(failed))
            except Exception:
                pass

        return {"invited": invited, "failed": failed, "batches": batches_done}

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("invite_users_to_channel error: %s", e)
        return {"invited": invited, "failed": failed, "batches": batches_done, "error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

async def join_channel_by_id(
    session_string: str,
    channel_id: int,
    access_hash: int = 0,
    _acc: dict | None = None,
) -> dict:
    """Вступить в канал по channel_id + access_hash. Preflight перед инвайтом.

    Возвращает {ok, tg_user_id, already_member, error?, flood_wait?}.
    Если аккаунт уже участник — ok=True, already_member=True.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import InputChannel
    from telethon.errors import FloodWaitError, UserBannedInChannelError, ChannelPrivateError

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=10.0)
        tg_user_id = me.id if me else 0

        try:
            if access_hash and channel_id:
                ch_input = InputChannel(channel_id=channel_id, access_hash=access_hash)
            else:
                ch_input = await asyncio.wait_for(client.get_entity(channel_id), timeout=10.0)
            await asyncio.wait_for(client(JoinChannelRequest(channel=ch_input)), timeout=20.0)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            return {"ok": True, "tg_user_id": tg_user_id, "already_member": False}
        except Exception as e:
            err = str(e)
            if "ALREADY_PARTICIPANT" in err.upper() or "already" in err.lower():
                return {"ok": True, "tg_user_id": tg_user_id, "already_member": True}
            if isinstance(e, FloodWaitError):
                return {"ok": False, "tg_user_id": tg_user_id,
                        "error": f"FloodWait {e.seconds}s", "flood_wait": e.seconds}
            if isinstance(e, UserBannedInChannelError):
                return {"ok": False, "tg_user_id": tg_user_id, "error": "забанен в канале"}
            if isinstance(e, ChannelPrivateError):
                return {"ok": False, "tg_user_id": tg_user_id, "error": "канал приватный"}
            return {"ok": False, "tg_user_id": tg_user_id, "error": err[:100]}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"ok": False, "tg_user_id": 0, "error": str(e)[:100]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def get_own_user_id(session_string: str, _acc: dict | None = None) -> int:
    """Return Telegram user id for a session, or 0 when the session is invalid."""
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=10.0)
        return int(me.id) if me else 0
    except Exception as e:
        log.warning("get_own_user_id error acc=%s: %s", (_acc or {}).get("id", "?"), e)
        return 0
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
            log_exc_swallow(log, "Сбой в get_contacts")

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
            log_exc_swallow(log, "Сбой в kick_from_channel")

async def promote_to_admin(
    session_string: str,
    channel_id: int | str,
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
    from telethon.tl.types import ChatAdminRights, PeerUser
    from telethon.errors import ChatAdminRequiredError, UserNotParticipantError

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        channel = await _resolve_channel_peer(client, channel_id, access_hash)

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
            log_exc_swallow(log, "Сбой в promote_to_admin")
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
            log_exc_swallow(log, "Сбой в post_to_channel")

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
            log_exc_swallow(log, "Сбой в send_reaction")

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
            log_exc_swallow(log, "Сбой в report_peer")

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
            log_exc_swallow(log, "Сбой в report_peer_deep")
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
                # Обновляем entity после join — access hash обновляется в сессии
                entity = await client.get_entity(peer_username.lstrip("@"))
                await asyncio.sleep(random.uniform(2.0, 4.5))
            except Exception as e:
                log.warning("report_peer_deep[3/join]: %s", e)

        # Get full channel info (linked group, about text)
        full_chat = None
        try:
            full_result = await client(GetFullChannelRequest(entity))
            full_chat = full_result.full_chat
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer_deep")
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
            log_exc_swallow(log, "Сбой в report_peer_deep")
        if result["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception:
                log_exc_swallow(log, "Сбой в report_peer_deep")
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
            log_exc_swallow(log, "Сбой в report_peer_deep")
    return result

async def report_peer_deep_v2(  # noqa: C901
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
    """Bulletproof 12-vector deep strike. Every vector isolated, entity refreshed after join."""
    import re as _re

    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.functions.messages import ReportRequest as MsgReportRequest
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import (
        JoinChannelRequest, LeaveChannelRequest, GetParticipantsRequest, GetFullChannelRequest,
    )
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence, InputReportReasonPornography,
        InputReportReasonChildAbuse, InputReportReasonCopyright, InputReportReasonOther,
        Channel, ChannelParticipantsAdmins, ReactionEmoji, InputMessagesFilterPinned,
        ReportResultAddComment, ReportResultChooseOption, ReportResultReported,
    )

    _RPP = None
    try:
        from telethon.tl.functions.account import ReportProfilePhotoRequest as _RPP
    except ImportError:
        pass

    _CSR = None
    try:
        from telethon.tl.functions.channels import ReportSpamRequest as _CSR
    except ImportError:
        pass

    # ── Result ────────────────────────────────────────────────────────────
    R: dict = {
        "peer_reported": False, "multi_reason_sent": 0, "photo_reported": False,
        "pinned_reported": 0, "msg_reported": 0, "msgs_fetched": 0, "spam_signaled": 0,
        "reactions_sent": 0, "admins_reported": 0, "linked_group_reported": False,
        "bots_reported": 0, "forwarded": 0, "blocked": False, "joined": False,
        "rate_limited": False, "errors": [],
    }

    # ── Reason map ────────────────────────────────────────────────────────
    _rm: dict = {
        "spam": InputReportReasonSpam(), "violence": InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(), "childabuse": InputReportReasonChildAbuse(),
        "copyright": InputReportReasonCopyright(), "other": InputReportReasonOther(),
    }
    for _tn, _rk in [("InputReportReasonIllegalDrugs", "drugs"),
                      ("InputReportReasonPersonalDetails", "personal"),
                      ("InputReportReasonFake", "fake"),
                      ("InputReportReasonGeoIrrelevant", "geo")]:
        try:
            import telethon.tl.types as _tlt
            _rm[_rk] = getattr(_tlt, _tn)()
        except Exception:
            pass

    _escalation: dict[str, list[str]] = {
        "childabuse": ["pornography", "violence", "drugs", "spam", "other"],
        "csam":       ["pornography", "violence", "drugs", "spam", "other"],
        "drugs":      ["childabuse", "violence", "spam", "other"],
        "violence":   ["childabuse", "spam", "drugs", "fake", "other"],
        "terrorism":  ["childabuse", "violence", "spam", "drugs", "other"],
        "pornography": ["childabuse", "spam", "other", "violence"],
        "escort":     ["pornography", "childabuse", "spam", "other"],
        "spam":       ["other", "violence", "personal", "fake"],
        "other":      ["spam", "violence", "pornography", "drugs"],
        "copyright":  ["spam", "other"],
        "fraud":      ["spam", "other", "fake", "violence"],
        "weapons":    ["violence", "spam", "other"],
        "darknet":    ["spam", "other", "drugs"],
    }
    _fwd_bots: dict[str, str] = {
        "childabuse": "stopCA", "csam": "stopCA", "drugs": "stopCA",
        "violence": "notoscam", "other": "notoscam", "spam": "notoscam",
        "pornography": "notoscam", "fraud": "notoscam",
        "escort": "notoscam",
    }

    tg_reason = _rm.get(reason, InputReportReasonOther())
    all_reasons: list = [tg_reason] + [_rm[k] for k in _escalation.get(reason, []) if k in _rm]
    msg_pool: list[str] = msg_messages or ([message] if message else [""])
    peer = peer_username.lstrip("@")
    acc_id = (_acc or {}).get("id", "?")

    def _record_error(stage: str, err: object) -> None:
        text = str(err)[:120]
        R["errors"].append(f"{stage}: {text}")
        if "FLOOD" in text.upper() or "TOO_MUCH" in text.upper():
            R["rate_limited"] = True

    def _flood(err: str, default: float = 30.0) -> float:
        m = _re.search(r'(\d+)', err)
        return min(120.0, float(m.group(1))) if m else default

    async def _timed(coro, timeout: float = 15.0):
        return await asyncio.wait_for(coro, timeout=timeout)

    def _select_report_option(options: list) -> bytes | None:
        hints = {
            "spam": ("spam", "спам"),
            "violence": ("violence", "violent", "насил", "жест"),
            "pornography": ("porn", "sexual", "adult", "порно", "сексу"),
            "childabuse": ("child", "minor", "children", "дет"),
            "copyright": ("copyright", "автор"),
            "drugs": ("drug", "нарко"),
            "personal": ("personal", "private", "личн"),
            "fake": ("fake", "scam", "fraud", "фейк", "мошен"),
            "other": ("other", "другое"),
        }.get(reason, ())
        for opt in options:
            text = (getattr(opt, "text", "") or "").lower()
            if any(hint in text for hint in hints):
                return getattr(opt, "option", None)
        if options:
            return getattr(options[0], "option", None)
        return None

    async def _report_message_ids(peer_obj, msg_ids: list[int], comment: str, stage: str) -> bool:
        try:
            first = await client(MsgReportRequest(peer=peer_obj, id=msg_ids, option=b"", message=""))
            if isinstance(first, ReportResultReported):
                return True
            if isinstance(first, ReportResultChooseOption):
                option = _select_report_option(first.options)
                if not option:
                    _record_error(stage, "Telegram returned no report option")
                    return False
                second = await client(MsgReportRequest(peer=peer_obj, id=msg_ids, option=option, message=comment))
                if isinstance(second, ReportResultReported):
                    return True
                if isinstance(second, ReportResultAddComment):
                    final = await client(MsgReportRequest(
                        peer=peer_obj, id=msg_ids, option=second.option, message=comment
                    ))
                    return isinstance(final, ReportResultReported)
                _record_error(stage, f"unexpected result {type(second).__name__}")
                return False
            if isinstance(first, ReportResultAddComment):
                final = await client(MsgReportRequest(
                    peer=peer_obj, id=msg_ids, option=first.option, message=comment
                ))
                return isinstance(final, ReportResultReported)
            _record_error(stage, f"unexpected result {type(first).__name__}")
            return False
        except Exception as e:
            _record_error(stage, e)
            raise

    client = _make_client(session_string, _acc)
    try:
        await _timed(client.connect(), _CONNECT_TIMEOUT)

        # Resolve entity — без этого вся атака невозможна
        try:
            entity = await _timed(client.get_entity(peer))
        except Exception as e:
            log.warning("rpv2[0/entity] acc=%s target=%s: %s", acc_id, peer, e)
            return R

        is_channel = isinstance(entity, Channel)
        log.info("rpv2 start acc=%s target=%s is_channel=%s wave=%d", acc_id, peer, is_channel, wave_num)

        # ── 1. ReportPeer (все причины) ───────────────────────────────
        reasons_to_send = all_reasons if multi_reason else [tg_reason]
        for idx, r_obj in enumerate(reasons_to_send):
            if idx > 0:
                await asyncio.sleep(random.betavariate(2, 5) * 2.0 + 0.3)
            try:
                await client(ReportPeerRequest(
                    peer=entity, reason=r_obj,
                    message=msg_pool[idx % len(msg_pool)],
                ))
                if idx == 0:
                    R["peer_reported"] = True
                else:
                    R["multi_reason_sent"] += 1
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err.upper():
                    await asyncio.sleep(_flood(err) + random.uniform(1, 3))
                    try:
                        await client(ReportPeerRequest(peer=entity, reason=r_obj,
                                                       message=msg_pool[idx % len(msg_pool)]))
                        if idx == 0:
                            R["peer_reported"] = True
                        else:
                            R["multi_reason_sent"] += 1
                    except Exception:
                        pass
                elif "REPORT_TOO_MUCH" in err.upper() or "too_many" in err.lower():
                    _record_error("peer", err)
                    break
                else:
                    log.warning("rpv2[1/peer idx=%d] acc=%s: %s", idx, acc_id, err[:100])
        log.info("rpv2[1] peer=%s multi=%d acc=%s", R["peer_reported"], R["multi_reason_sent"], acc_id)
        await asyncio.sleep(random.uniform(0.5, 1.5))

        # ── 2. Фото профиля ───────────────────────────────────────────
        if report_photo and _RPP:
            try:
                photos = await _timed(client.get_profile_photos(entity, limit=1), 10.0)
                if photos:
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                    _p = photos[0]
                    # Не используем client._get_input_photo (приватный метод).
                    # Строим InputPhoto напрямую из атрибутов объекта.
                    from telethon.tl.types import InputPhoto as _InputPhoto
                    _photo_input = _InputPhoto(
                        id=_p.id,
                        access_hash=_p.access_hash,
                        file_reference=_p.file_reference,
                    )
                    await client(_RPP(
                        peer=entity, photo_id=_photo_input,
                        reason=tg_reason, message=msg_pool[0],
                    ))
                    R["photo_reported"] = True
                    log.info("rpv2[2/photo] reported acc=%s", acc_id)
            except Exception as e:
                log.warning("rpv2[2/photo] acc=%s: %s", acc_id, str(e)[:80])

        # ── 2.5. Pre-fetch history (АНОНИМНО, до вступления) ─────────
        # Публичные каналы читаемы без вступления. Получаем историю ДО join-а,
        # чтобы обойти anti-bot защиту (CAS/ComBot), банящую новых участников.
        _prefetch_msgs: list = []
        if is_channel:
            try:
                from telethon.tl.functions.messages import GetHistoryRequest as _GHR_PRE
                _pre_hist = await _timed(client(_GHR_PRE(
                    peer=entity,
                    offset_id=0, offset_date=0, add_offset=0,
                    limit=max_msg_reports, max_id=0, min_id=0, hash=0,
                )), 20.0)
                _prefetch_msgs = [
                    m for m in getattr(_pre_hist, "messages", [])
                    if m and m.id and not getattr(m, "action", None)
                ]
                if _prefetch_msgs:
                    log.info("rpv2[2.5] anon_prefetch=%d acc=%s target=%s",
                             len(_prefetch_msgs), acc_id, peer)
            except Exception as _pre_e:
                log.debug("rpv2[2.5] skipped acc=%s: %s", acc_id, str(_pre_e)[:60])

        # ── 3. Join channel — ОБЯЗАТЕЛЬНО до message reporting ────────
        if join_first and is_channel:
            _need_refresh = True   # нужен ли дополнительный entity-refresh
            try:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                _join_resp = await _timed(client(JoinChannelRequest(entity)))
                R["joined"] = True
                log.info("rpv2[3] joined acc=%s target=%s", acc_id, peer)
                # PRIMARY: ответ JoinChannelRequest содержит актуальный entity из сервера —
                # это надёжнее отдельного GetChannelsRequest (нет проблем с кэшем).
                _fresh = getattr(_join_resp, "chats", [])
                if _fresh:
                    # Ищем наш канал по ID — в chats[1] может быть linked group
                    _matched = next(
                        (c for c in _fresh if getattr(c, "id", None) == entity.id),
                        _fresh[0]
                    )
                    entity = _matched
                    _need_refresh = False
                    log.info("rpv2[3] entity from join_resp acc=%s ah=%s",
                             acc_id, getattr(entity, "access_hash", "?"))
                await asyncio.sleep(random.uniform(3.0, 7.0) if wave_num == 0 else random.uniform(1.0, 2.5))
            except Exception as e:
                err = str(e)
                if "ALREADY_PARTICIPANT" in err.upper() or "already" in err.lower():
                    R["joined"] = True
                    log.info("rpv2[3] already_participant acc=%s target=%s", acc_id, peer)
                else:
                    log.warning("rpv2[3/join] acc=%s target=%s: %s", acc_id, peer, err[:100])
            # FALLBACK refresh: когда join не дал свежий entity (join_resp.chats пустой,
            # ALREADY_PARTICIPANT, или join упал).
            # Используем get_input_entity — берёт access_hash из сессии (надёжнее для членов).
            if _need_refresh:
                try:
                    from telethon.tl.functions.channels import GetChannelsRequest as _GCR
                    # get_input_entity для канала в котором аккаунт уже состоит
                    # возвращает InputChannel из session cache с правильным access_hash
                    _ie = None
                    try:
                        _ie = await _timed(client.get_input_entity(peer), 5.0)
                    except Exception as _gie:
                        log.warning("rpv2[3/gie] acc=%s: %s", acc_id, str(_gie)[:60])
                        from telethon.tl.types import InputChannel as _IC
                        _ie = _IC(entity.id, entity.access_hash)
                    _gcr = await _timed(client(_GCR([_ie])), 10.0)
                    if _gcr and _gcr.chats:
                        entity = _gcr.chats[0]
                        log.info("rpv2[3/gcr] entity refreshed acc=%s ah=%s",
                                 acc_id, getattr(entity, "access_hash", "?"))
                    else:
                        raise ValueError("gcr empty")
                except Exception as _gcr_e:
                    log.warning("rpv2[3/gcr] acc=%s: %s — fallback get_entity", acc_id, str(_gcr_e)[:80])
                    try:
                        entity = await _timed(client.get_entity(peer), 8.0)
                        log.info("rpv2[3/get_entity] acc=%s ah=%s",
                                 acc_id, getattr(entity, "access_hash", "?"))
                    except Exception as e2:
                        log.warning("rpv2[3/get_entity] acc=%s: %s", acc_id, str(e2)[:80])

        # ── 4. Full channel info ───────────────────────────────────────
        full_chat = None
        if is_channel:
            try:
                fc_res = await _timed(client(GetFullChannelRequest(entity)))
                full_chat = fc_res.full_chat
                # GetFullChannelRequest тоже возвращает chats — ещё один refresh point
                _fc_chats = getattr(fc_res, "chats", [])
                if _fc_chats:
                    _fc_match = next(
                        (c for c in _fc_chats if getattr(c, "id", None) == entity.id),
                        _fc_chats[0]
                    )
                    entity = _fc_match
                    log.info("rpv2[4] entity from GetFullChannel acc=%s ah=%s",
                             acc_id, getattr(entity, "access_hash", "?"))
            except Exception as e:
                log.warning("rpv2[4/full] acc=%s: %s", acc_id, str(e)[:80])

        # ── 5. Pinned messages ────────────────────────────────────────
        if report_pinned and is_channel:
            try:
                from telethon.tl.types import InputPeerChannel as _IPC5
                _ipeer5 = _IPC5(channel_id=entity.id, access_hash=entity.access_hash)
                pinned = await _timed(
                    client.get_messages(_ipeer5, filter=InputMessagesFilterPinned(), limit=25), 15.0
                )
                pinned_ids = [m.id for m in pinned if m and m.id]
                log.info("rpv2[5] pinned=%d acc=%s", len(pinned_ids), acc_id)
                for ip, pid in enumerate(pinned_ids):
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 1.5 + 0.3)
                        ok = await _report_message_ids(
                            entity, [pid], msg_pool[ip % len(msg_pool)], f"pin:{pid}"
                        )
                        if ok:
                            R["pinned_reported"] += 1
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_flood(err, 15))
                        elif "REPORT_TOO_MUCH" in err.upper():
                            break
                        else:
                            log.warning("rpv2[5/pin] acc=%s pid=%d: %s", acc_id, pid, err[:80])
            except Exception as e:
                log.warning("rpv2[5/get_pinned] acc=%s: %s", acc_id, str(e)[:80])

        # ── 6. Recent messages ────────────────────────────────────────
        # Используем GetHistoryRequest с явным InputPeerChannel — обходит кэш Telethon.
        # После JoinChannelRequest client.get_messages(entity) может вернуть 0 сообщений,
        # если entity устарел. Явный InputPeerChannel(id, access_hash) гарантирует
        # прямой запрос к серверу с актуальным access_hash.
        msgs: list = []
        if is_channel:
            from telethon.tl.functions.messages import GetHistoryRequest as _GHR
            from telethon.tl.types import InputPeerChannel as _IPC6
            _ah6 = getattr(entity, "access_hash", 0)
            log.info("rpv2[6] building InputPeerChannel id=%s ah=%s joined=%s acc=%s",
                     entity.id, _ah6, R["joined"], acc_id)
            _ipeer6 = _IPC6(channel_id=entity.id, access_hash=_ah6)
            try:
                _hist = await _timed(client(_GHR(
                    peer=_ipeer6,
                    offset_id=0, offset_date=0, add_offset=0,
                    limit=max_msg_reports, max_id=0, min_id=0, hash=0,
                )), 25.0)
                msgs = [m for m in getattr(_hist, "messages", [])
                        if m and m.id and not getattr(m, "action", None)]
                log.info("rpv2[6/GetHistory] fetched=%d target=%s acc=%s joined=%s ah=%s",
                         len(msgs), peer, acc_id, R["joined"], _ah6)
            except Exception as _gh_e:
                log.warning("rpv2[6/GetHistory] acc=%s ah=%s: %s — fallback get_messages",
                            acc_id, _ah6, str(_gh_e)[:80])
                try:
                    raw = await _timed(client.get_messages(_ipeer6, limit=max_msg_reports), 20.0)
                    msgs = [m for m in (raw or []) if m and m.id and not getattr(m, "action", None)]
                    log.info("rpv2[6/fallback] fetched=%d acc=%s", len(msgs), acc_id)
                except Exception as _fb_e:
                    log.warning("rpv2[6/fallback] acc=%s: %s", acc_id, str(_fb_e)[:80])
            # Третий fallback: entity напрямую (Telethon резолвит сам без InputPeerChannel)
            if not msgs:
                try:
                    raw2 = await _timed(client.get_messages(entity, limit=max_msg_reports), 20.0)
                    msgs = [m for m in (raw2 or []) if m and not getattr(m, "action", None)]
                    if msgs:
                        log.info("rpv2[6/entity_fb] fetched=%d acc=%s", len(msgs), acc_id)
                except Exception as _ef:
                    log.warning("rpv2[6/entity_fb] acc=%s: %s", acc_id, str(_ef)[:80])
            # Четвёртый fallback: анонимный pre-fetch (до вступления — обходит ban-on-join)
            if not msgs and _prefetch_msgs:
                msgs = _prefetch_msgs
                log.info("rpv2[6/pre_fallback] using anon pre-join msgs=%d acc=%s", len(msgs), acc_id)

            R["msgs_fetched"] = len(msgs)
            msg_ids = [m.id for m in msgs if m and m.id]
            log.info("rpv2[6] msg_ids=%d target=%s acc=%s joined=%s",
                     len(msg_ids), peer, acc_id, R["joined"])
            if not msg_ids:
                log.warning("rpv2[6] 0 msgs target=%s acc=%s — channel may restrict history", peer, acc_id)

            chunks = [msg_ids[i:i + 5] for i in range(0, len(msg_ids), 5)]
            random.shuffle(chunks)
            for ci, chunk in enumerate(chunks):
                cmsg = msg_pool[ci % len(msg_pool)]
                try:
                    ok = await _report_message_ids(_ipeer6, chunk, cmsg, f"msg_chunk:{ci}")
                    if ok:
                        R["msg_reported"] += len(chunk)
                except Exception as e:
                    err = str(e)
                    if "FLOOD_WAIT" in err.upper():
                        await asyncio.sleep(_flood(err, 15))
                        try:
                            ok = await _report_message_ids(_ipeer6, chunk[:2], cmsg, f"msg_retry:{ci}")
                            if ok:
                                R["msg_reported"] += min(2, len(chunk))
                        except Exception:
                            pass
                    elif "REPORT_TOO_MUCH" in err.upper():
                        log.info("rpv2[6] REPORT_TOO_MUCH ci=%d, stopping", ci)
                        break
                    else:
                        log.warning("rpv2[6/chunk ci=%d] acc=%s: %s", ci, acc_id, err[:100])
                await asyncio.sleep(random.betavariate(2, 5) * 1.2 + 0.3)
            log.info("rpv2[6] msg_reported=%d acc=%s", R["msg_reported"], acc_id)

        # ── 7. channels.ReportSpam ────────────────────────────────────
        # channels.reportSpam(channel, participant, ids) requires a USER as participant.
        # We collect message senders from the fetched msgs to use as participant.
        if _CSR and msgs and is_channel:
            spam_ids = [m.id for m in msgs[:15] if m and m.id]
            # Find a sender entity: prefer messages with from_id (non-anonymous posts)
            _spam_participant = None
            for _sm in msgs[:10]:
                _fid = getattr(_sm, "from_id", None)
                if _fid is not None:
                    try:
                        _spam_participant = await _timed(client.get_entity(_fid), 8.0)
                        break
                    except Exception:
                        pass
            if spam_ids and _spam_participant:
                try:
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                    await client(_CSR(channel=entity, participant=_spam_participant, id=spam_ids))
                    R["spam_signaled"] += len(spam_ids)
                    log.info("rpv2[7] spam_signaled=%d acc=%s", len(spam_ids), acc_id)
                except Exception as e:
                    log.warning("rpv2[7/spam] acc=%s: %s", acc_id, str(e)[:80])

        # ── 8. Negative reactions ─────────────────────────────────────
        if negative_react and msgs:
            pools = [["👎", "💩", "🤮"], ["👎", "🤬", "💩"], ["👎", "🤮"], ["💩", "🤬"], ["👎"]]
            rpool = pools[wave_num % len(pools)]
            for ri, m in enumerate(msgs[:20]):
                if not (m and m.id):
                    continue
                try:
                    await client(SendReactionRequest(
                        peer=entity, msg_id=m.id,
                        reaction=[ReactionEmoji(emoticon=rpool[ri % len(rpool)])],
                    ))
                    R["reactions_sent"] += 1
                    await asyncio.sleep(random.betavariate(2, 4) * 0.8 + 0.15)
                except Exception as e:
                    log.warning("rpv2[8/react] acc=%s: %s", acc_id, str(e)[:60])

        # ── 9. Report admins ──────────────────────────────────────────
        if report_admins and is_channel:
            try:
                adm = await _timed(client(GetParticipantsRequest(
                    channel=entity, filter=ChannelParticipantsAdmins(),
                    offset=0, limit=50, hash=0,
                )))
                admins = list(getattr(adm, "users", []))
                random.shuffle(admins)
                log.info("rpv2[9] admins=%d target=%s acc=%s", len(admins), peer, acc_id)
                for ai, usr in enumerate(admins):
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 1.0 + 0.3)
                        await client(ReportPeerRequest(
                            peer=usr, reason=all_reasons[ai % len(all_reasons)],
                            message=msg_pool[ai % len(msg_pool)],
                        ))
                        R["admins_reported"] += 1
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_flood(err, 10))
                        else:
                            log.warning("rpv2[9/admin ai=%d] acc=%s: %s", ai, acc_id, err[:80])
            except Exception as e:
                log.warning("rpv2[9/get_admins] acc=%s: %s", acc_id, str(e)[:80])

        # ── 10. Linked discussion group ────────────────────────────────
        if report_linked_group and full_chat:
            linked_id = getattr(full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    lent = await _timed(client.get_entity(int(linked_id)), 10.0)
                    for li in range(min(4, len(all_reasons))):
                        try:
                            await asyncio.sleep(random.betavariate(2, 5) * 1.2 + 0.4)
                            await client(ReportPeerRequest(
                                peer=lent, reason=all_reasons[li],
                                message=msg_pool[li % len(msg_pool)],
                            ))
                            R["linked_group_reported"] = True
                        except Exception as e:
                            log.warning("rpv2[10/linked li=%d] acc=%s: %s", li, acc_id, str(e)[:80])
                except Exception as e:
                    log.warning("rpv2[10/get_linked] acc=%s: %s", acc_id, str(e)[:80])

        # ── 11. Linked bots ────────────────────────────────────────────
        if report_linked_bots and is_channel:
            bot_re = _re.compile(r'@([A-Za-z]\w{4,31}[Bb]ot)\b')
            scan = (getattr(full_chat, "about", "") or "") if full_chat else ""
            for m in msgs[:10]:
                if m and m.text:
                    scan += " " + m.text
            for bi, bname in enumerate(list(set(bot_re.findall(scan)))[:6]):
                try:
                    bent = await _timed(client.get_entity(bname), 8.0)
                    await client(ReportPeerRequest(
                        peer=bent, reason=all_reasons[bi % len(all_reasons)],
                        message=msg_pool[bi % len(msg_pool)],
                    ))
                    R["bots_reported"] += 1
                    await asyncio.sleep(random.uniform(0.4, 1.2))
                except Exception as e:
                    log.warning("rpv2[11/bot %s] acc=%s: %s", bname, acc_id, str(e)[:80])

        # ── 12. Forward evidence ───────────────────────────────────────
        if forward_to_bot and msgs:
            bot_uname = _fwd_bots.get(reason, "notoscam")
            try:
                fbot = await _timed(client.get_entity(bot_uname), 8.0)
                for em in [m for m in msgs[:8] if m and not m.service]:
                    try:
                        await client.forward_messages(fbot, em)
                        R["forwarded"] += 1
                        await asyncio.sleep(random.uniform(0.3, 1.0))
                    except Exception as e:
                        log.warning("rpv2[12/fwd] acc=%s: %s", acc_id, str(e)[:60])
            except Exception as e:
                log.warning("rpv2[12/fbot] acc=%s: %s", acc_id, str(e)[:80])

        # ── 13. Mute + Leave + Block ───────────────────────────────────
        try:
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=entity),
                settings=InputPeerNotifySettings(mute_until=2_147_483_647),
            ))
        except Exception:
            pass
        if R["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception:
                pass
        if block_after:
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await client(BlockRequest(id=entity))
                R["blocked"] = True
            except Exception as e:
                log.warning("rpv2[13/block] acc=%s: %s", acc_id, str(e)[:80])

        log.info("rpv2 DONE acc=%s target=%s | peer=%s msgs=%d admins=%d pinned=%d joined=%s",
                 acc_id, peer, R["peer_reported"], R["msg_reported"],
                 R["admins_reported"], R["pinned_reported"], R["joined"])

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("rpv2 FATAL acc=%s target=%s: %s", acc_id, peer, e)
        R["_fatal_error"] = str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    return R



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

        # Полная инфо о канале (описание, linked_chat_id) + refresh entity
        try:
            full = await client(GetFullChannelRequest(entity))
            fc = full.full_chat
            intel["description"]    = (getattr(fc, "about", "") or "")[:500]
            intel["linked_group_id"] = getattr(fc, "linked_chat_id", None)
            # Refresh entity из ответа (более актуальный access_hash)
            _fc = next(
                (c for c in getattr(full, "chats", []) if getattr(c, "id", None) == entity.id),
                None
            )
            if _fc:
                entity = _fc
                intel["access_hash"] = getattr(entity, "access_hash", 0) or 0
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Все администраторы (до 200)
        try:
            adm = await client(GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsAdmins(),
                offset=0, limit=200, hash=0,
            ))
            intel["admin_ids"] = [u.id for u in getattr(adm, "users", [])]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Закреплённые сообщения
        try:
            pinned = await client.get_messages(entity, filter=InputMessagesFilterPinned(), limit=20)
            intel["pinned_msg_ids"] = [m.id for m in pinned if m and m.id]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Последние 100 сообщений
        try:
            msgs = await client.get_messages(entity, limit=100)
            intel["latest_msg_ids"] = [m.id for m in msgs if m and m.id]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Упомянутые @usernames и @botы из описания + последних постов
        scan_text = intel["description"]
        try:
            msgs_text = await client.get_messages(entity, limit=15)
            for m in msgs_text:
                if m and m.text:
                    scan_text += " " + m.text
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
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
            log_exc_swallow(log, "Сбой в strike_map_target")
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
            log_exc_swallow(log, "Сбой в update_profile")

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
            log_exc_swallow(log, "Сбой в update_account_username")
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
            log_exc_swallow(log, "Сбой в _bf_send_with_retry")
