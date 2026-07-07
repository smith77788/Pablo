"""Движок массового оформления аккаунтов.

Возможности:
  - Имя / фамилия / bio
  - Аватар (URL или байты)
  - Username
  - 2FA пароль (установить / изменить)

Каждая функция принимает session_string + _acc dict и выполняет одно действие.
Вызывается из op_worker._exec_bulk_set_profile.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 12.0


async def _connect(session_string: str, _acc: dict | None):
    from services.account_manager import _make_client
    client = _make_client(session_string, _acc)
    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
    return client


# ── Имя / фамилия / bio ───────────────────────────────────────────────────────

async def set_name_bio(
    session_string: str,
    _acc: dict | None,
    first_name: str = "",
    last_name: str = "",
    about: str = "",
) -> dict[str, Any]:
    from telethon.tl.functions.account import UpdateProfileRequest

    client = await _connect(session_string, _acc)
    try:
        kwargs: dict = {}
        if first_name:
            kwargs["first_name"] = first_name[:64]
        if last_name is not None:
            kwargs["last_name"] = last_name[:64]
        if about is not None:
            kwargs["about"] = about[:70]
        await asyncio.wait_for(
            client(UpdateProfileRequest(**kwargs)),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Аватар из URL ─────────────────────────────────────────────────────────────

async def set_avatar_from_url(
    session_string: str,
    _acc: dict | None,
    photo_url: str,
) -> dict[str, Any]:
    import aiohttp
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    client = await _connect(session_string, _acc)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(photo_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return {"ok": False, "error": f"HTTP {resp.status} downloading photo"}
                data = await resp.read()

        file = await asyncio.wait_for(
            client.upload_file(data, file_name="avatar.jpg"),
            timeout=30.0,
        )
        await asyncio.wait_for(
            client(UploadProfilePhotoRequest(file=file)),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Аватар из байтов ──────────────────────────────────────────────────────────

async def set_avatar_from_bytes(
    session_string: str,
    _acc: dict | None,
    photo_bytes: bytes,
    filename: str = "avatar.jpg",
) -> dict[str, Any]:
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    client = await _connect(session_string, _acc)
    try:
        file = await asyncio.wait_for(
            client.upload_file(photo_bytes, file_name=filename),
            timeout=30.0,
        )
        await asyncio.wait_for(
            client(UploadProfilePhotoRequest(file=file)),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Username ──────────────────────────────────────────────────────────────────

async def set_username(
    session_string: str,
    _acc: dict | None,
    username: str,
) -> dict[str, Any]:
    from telethon.tl.functions.account import UpdateUsernameRequest

    client = await _connect(session_string, _acc)
    try:
        await asyncio.wait_for(
            client(UpdateUsernameRequest(username=username)),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── 2FA пароль ────────────────────────────────────────────────────────────────

async def set_2fa_password(
    session_string: str,
    _acc: dict | None,
    new_password: str,
    current_password: str = "",
    hint: str = "",
) -> dict[str, Any]:
    """Установить или изменить 2FA пароль аккаунта."""
    from telethon.tl.functions.account import GetPasswordRequest, UpdatePasswordSettingsRequest
    from telethon.tl.types import (
        PasswordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow,
        account,
    )

    client = await _connect(session_string, _acc)
    try:
        # Используем встроенный edit_2fa метода telethon если есть
        await asyncio.wait_for(
            client.edit_2fa(
                current_password=current_password or None,
                new_password=new_password,
                hint=hint,
            ),
            timeout=30.0,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Безопасность: закрыть сторонние сессии ────────────────────────────────────

async def close_other_sessions(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Завершить ВСЕ прочие авторизации аккаунта (кроме текущей).
    Аналог «Закрыть сторонние сессии» — защита угнанных/прогретых аккаунтов."""
    from telethon.tl.functions.auth import ResetAuthorizationsRequest

    client = await _connect(session_string, _acc)
    try:
        await asyncio.wait_for(client(ResetAuthorizationsRequest()), timeout=_ACTION_TIMEOUT)
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Настройки приватности ─────────────────────────────────────────────────────

# key → (PrivacyKey, разрешено/запрещено). value=True = «открыть/показать всем»,
# value=False = «скрыть/закрыть» (никому).
_PRIVACY_KEYS = {"phone", "invite", "lastseen"}


async def set_privacy(session_string: str, _acc: dict | None, key: str, allow: bool) -> dict[str, Any]:
    """Настройка приватности: phone (номер), invite (кто может добавлять в группы),
    lastseen (был в сети). allow=True — всем, False — никому."""
    from telethon.tl.functions.account import SetPrivacyRequest
    from telethon.tl.types import (
        InputPrivacyKeyPhoneNumber, InputPrivacyKeyChatInvite, InputPrivacyKeyStatusTimestamp,
        InputPrivacyValueAllowAll, InputPrivacyValueDisallowAll,
    )
    keymap = {
        "phone": InputPrivacyKeyPhoneNumber,
        "invite": InputPrivacyKeyChatInvite,
        "lastseen": InputPrivacyKeyStatusTimestamp,
    }
    if key not in keymap:
        return {"ok": False, "error": f"unknown privacy key {key}"}
    client = await _connect(session_string, _acc)
    try:
        rule = InputPrivacyValueAllowAll() if allow else InputPrivacyValueDisallowAll()
        await asyncio.wait_for(
            client(SetPrivacyRequest(key=keymap[key](), rules=[rule])),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Получить код авторизации ──────────────────────────────────────────────────

def extract_login_code(text: str) -> str | None:
    """Выделить 5–6-значный код входа Telegram из текста сервисного сообщения.
    Сначала ищем рядом со словом code/код (устойчивее к номерам/датам в тексте),
    затем — любой изолированный 5–6-значный блок. Чистая функция (без Telethon)."""
    if not text:
        return None
    match = re.search(r"(?:code|код)[^\d]{0,20}(\d{5,6})", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(\d{5,6})\b", text)
    return match.group(1) if match else None


async def get_login_code(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Прочитать последний код входа Telegram из служебного чата (777000).
    Возвращает {ok, code, error}. Аналог «Получить код авторизации»."""
    client = await _connect(session_string, _acc)
    try:
        msgs = await asyncio.wait_for(client.get_messages(777000, limit=5), timeout=_ACTION_TIMEOUT)
        for m in (msgs or []):
            code = extract_login_code(getattr(m, "message", "") or "")
            if code:
                return {"ok": True, "code": code, "error": None}
        return {"ok": False, "code": None, "error": "Код не найден в последних сообщениях"}
    except Exception as exc:
        return {"ok": False, "code": None, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── Спинтакс (рандомизация текста) ───────────────────────────────────────────

def expand_spintax(text: str) -> str:
    """Раскрыть {вариант1|вариант2|вариант3} → случайный вариант."""
    import random

    def _replace(m: re.Match) -> str:
        options = m.group(1).split("|")
        return random.choice(options)

    result = text
    while "{" in result and "|" in result:
        new = re.sub(r"\{([^{}]+)\}", _replace, result)
        if new == result:
            break
        result = new
    return result
