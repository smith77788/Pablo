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
