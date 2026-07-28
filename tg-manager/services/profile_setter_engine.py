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
from services.logger import log_exc_swallow

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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "set_name_bio: disconnect")


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "set_avatar_from_url: disconnect")


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "set_avatar_from_bytes: disconnect")


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "set_username: disconnect")


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log_exc_swallow(log, "set_2fa_password: disconnect")


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: close_other_sessions disconnect failed: %s', e)


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: set_privacy disconnect failed: %s', e)


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
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "code": None, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: get_login_code disconnect failed: %s', e)


# ── Сброс/удаление полей профиля ──────────────────────────────────────────────

async def clear_bio(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Очистить bio (О себе). Аналог «Удалить bio» Telegram Expert."""
    from telethon.tl.functions.account import UpdateProfileRequest

    client = await _connect(session_string, _acc)
    try:
        await asyncio.wait_for(
            client(UpdateProfileRequest(about="")), timeout=_ACTION_TIMEOUT
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: clear_bio disconnect failed: %s', e)


async def remove_username(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Снять @username аккаунта (пустая строка). Аналог «Удалить username»."""
    return await set_username(session_string, _acc, "")


async def remove_avatar(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Удалить ВСЕ фото профиля. Аналог «Удалить фото» Telegram Expert."""
    from telethon import utils
    from telethon.tl.functions.photos import DeletePhotosRequest

    client = await _connect(session_string, _acc)
    try:
        photos = await asyncio.wait_for(
            client.get_profile_photos("me"), timeout=_ACTION_TIMEOUT
        )
        if not photos:
            return {"ok": True, "error": None, "note": "нет фото"}
        ids = [utils.get_input_photo(p) for p in photos]
        await asyncio.wait_for(client(DeletePhotosRequest(id=ids)), timeout=_ACTION_TIMEOUT)
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: remove_avatar disconnect failed: %s', e)


# ── Сброс 2FA (снять пароль) ──────────────────────────────────────────────────

async def reset_2fa(
    session_string: str, _acc: dict | None, current_password: str
) -> dict[str, Any]:
    """Снять 2FA-пароль (нужен текущий пароль). Аналог «Сбросить/удалить 2FA»."""
    client = await _connect(session_string, _acc)
    try:
        if not current_password:
            return {"ok": False, "error": "нужен текущий пароль для снятия 2FA"}
        await asyncio.wait_for(
            client.edit_2fa(current_password=current_password, new_password=None),
            timeout=30.0,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: reset_2fa disconnect failed: %s', e)


# ── Держать онлайн (разовый пинг) ─────────────────────────────────────────────

async def set_online(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Выставить статус «в сети» (offline=False). Разовый пинг присутствия.
    Непрерывный keep-online — задача планировщика (см. backlog)."""
    from telethon.tl.functions.account import UpdateStatusRequest

    client = await _connect(session_string, _acc)
    try:
        await asyncio.wait_for(
            client(UpdateStatusRequest(offline=False)), timeout=_ACTION_TIMEOUT
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: set_online disconnect failed: %s', e)


# ── Проверка ограничений (бан / restricted / deleted) ─────────────────────────

async def check_restriction(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Проверить состояние аккаунта: жив / ограничен / удалён.
    Аналог раздела «ПРОВЕРКА» Telegram Expert. Возвращает
    {ok, alive, restricted, deleted, reason, username, user_id}."""
    client = await _connect(session_string, _acc)
    try:
        me = await asyncio.wait_for(client.get_me(), timeout=_ACTION_TIMEOUT)
        if me is None:
            return {"ok": True, "alive": False, "restricted": False,
                    "deleted": True, "reason": "сессия не авторизована",
                    "username": None, "user_id": None}
        reasons = getattr(me, "restriction_reason", None) or []
        reason_text = "; ".join(
            f"{getattr(r, 'platform', '')}:{getattr(r, 'text', '')}" for r in reasons
        ) if reasons else None
        return {
            "ok": True,
            "alive": True,
            "restricted": bool(getattr(me, "restricted", False)),
            "deleted": bool(getattr(me, "deleted", False)),
            "reason": reason_text,
            "username": getattr(me, "username", None),
            "user_id": getattr(me, "id", None),
        }
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "alive": False, "restricted": False,
                "deleted": False, "reason": None, "error": str(exc)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning('profile_setter: check_restriction disconnect failed: %s', e)


def format_restriction_verdict(res: dict) -> str:
    """Свернуть результат check_restriction в человекочитаемый вердикт.
    Чистая функция (без Telethon) — тестируется отдельно от сетевого пути."""
    if res.get("deleted"):
        return "❌ удалён/не авторизован"
    if res.get("restricted"):
        return "⛔ ограничен: " + (res.get("reason") or "без причины")
    return "✅ жив, без ограничений"


# ── Спинтакс (рандомизация текста) ───────────────────────────────────────────

def expand_spintax(text: str) -> str:
    """Раскрыть {вариант1|вариант2|вариант3} → случайный вариант.

    Тонкая обёртка над общей реализацией: третья копия одного и того же
    разбора расходилась бы с остальными по вложенности и по поведению на
    битых шаблонах, а профили ставятся тем же движком массовых операций.
    """
    from services.dm_engine import expand_spintax as _expand

    return _expand(text)


async def apply_op(session_string: str, acc: dict, op: str, params: dict) -> dict:
    """Единый диспетчер аккаунт-операции (op → движковая функция).

    ОДНА реализация для очереди (op_worker._exec_bulk_set_profile) и инлайн-пути
    (mini_app account_profile) — без дублей. Возвращает {ok, error, ...}.
    """
    if op == "name":
        nd = params.get("name_data", {})
        return await set_name_bio(
            session_string, acc,
            expand_spintax(nd.get("first_name", "")),
            expand_spintax(nd.get("last_name", "")),
            expand_spintax(nd.get("about", "")),
        )
    if op == "avatar":
        return await set_avatar_from_url(session_string, acc, params.get("avatar_url", ""))
    if op == "2fa":
        return await set_2fa_password(
            session_string, acc,
            new_password=params.get("new_password", ""),
            current_password=params.get("current_password", ""),
            hint=params.get("hint", ""),
        )
    if op == "username":
        return await set_username(session_string, acc, expand_spintax(params.get("username", "")))
    if op == "close_sessions":
        return await close_other_sessions(session_string, acc)
    if op == "privacy":
        return await set_privacy(
            session_string, acc,
            key=params.get("privacy_key", "phone"),
            allow=bool(params.get("privacy_allow", False)),
        )
    if op == "clear_bio":
        return await clear_bio(session_string, acc)
    if op == "remove_username":
        return await remove_username(session_string, acc)
    if op == "remove_avatar":
        return await remove_avatar(session_string, acc)
    if op == "reset_2fa":
        return await reset_2fa(session_string, acc, current_password=params.get("current_password", ""))
    if op == "set_online":
        return await set_online(session_string, acc)
    if op == "check_restriction":
        return await check_restriction(session_string, acc)
    return {"ok": False, "error": f"unknown op: {op}"}
