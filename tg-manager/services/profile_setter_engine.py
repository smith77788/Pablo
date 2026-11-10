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


class ConnectFailed(Exception):
    """Клиент не поднялся. Текст исключения — уже готовое сообщение владельцу."""


def _connect_error_text(exc: BaseException) -> str:
    """Причина несостоявшегося коннекта человеческим языком.

    Владелец видит этот текст в мини-аппе и в сводке операции, поэтому
    «SessionBusyError» и «TimeoutError» здесь не годятся: по ним непонятно,
    ждать, чинить прокси или переподключать аккаунт.
    """
    name = type(exc).__name__
    if name == "SessionBusyError":
        return ("Сессия аккаунта занята другой операцией или прогревом — "
                "повторите через минуту")
    if name == "ProxyIsolationError":
        return ("У аккаунта не назначен прокси, а политика изоляции запрещает "
                "прямой выход — назначьте прокси аккаунту")
    if isinstance(exc, asyncio.TimeoutError):
        return (f"Аккаунт не ответил за {_CONNECT_TIMEOUT:.0f}с — "
                "проверьте прокси и живость сессии")
    if name in ("AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
                "SessionRevokedError", "SessionExpiredError",
                "UserDeactivatedBanError", "UserDeactivatedError"):
        return "Сессия аккаунта недействительна — нужен повторный вход"
    return f"Не удалось подключиться к Telegram: {str(exc)[:120]}"


async def _connect(session_string: str, _acc: dict | None):
    """Поднять клиента — либо ConnectFailed с понятной причиной.

    Коннект вызывался ДО try вызывающей функции, поэтому его сбой уходил наверх
    сырым исключением вместо ``{"ok": false, "error": ...}``, а полуподнятый
    клиент никто не отключал: он держал сокет и — через мьютекс сессии — сам
    аккаунт до протухания записи. Следующая подсистема получала «сессия занята»
    на живом аккаунте.
    """
    from services.account_manager import _make_client
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
    except BaseException as exc:
        try:
            await _safe_disconnect(client)
        except Exception:
            log_exc_swallow(log, "_connect: disconnect после сбоя коннекта")
        raise ConnectFailed(_connect_error_text(exc)) from exc
    return client


# ── Имя / фамилия / bio ───────────────────────────────────────────────────────

async def set_name_bio(
    session_string: str,
    _acc: dict | None,
    first_name: str = "",
    last_name: str | None = "",
    about: str | None = "",
) -> dict[str, Any]:
    """Имя, фамилия, описание. ``None`` в поле — не трогать его."""
    from telethon.tl.functions.account import UpdateProfileRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
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

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
    try:
        from services.security import public_http_session, read_capped

        # Адрес аватара приходит от пользователя: соединение только на
        # публичные адреса (и после переадресаций), размер — с потолком.
        async with public_http_session() as http:
            async with http.get(photo_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return {"ok": False, "error": f"HTTP {resp.status} downloading photo"}
                data = await read_capped(resp, 10 * 1024 * 1024)

        # Уникализация: один и тот же avatar_url на весь флот = байт-в-байт
        # идентичное фото профиля у всех аккаунтов, тривиальная кластеризация
        # фермы. Даём каждому аккаунту незаметно-уникальный вариант (пиксельный/
        # байтовый джиттер). Fail-safe: при сбое вернутся исходные байты.
        try:
            from services import media_uniquifier as _mu
            data = _mu.uniquify(data, filename="avatar.jpg")
        except Exception:
            log_exc_swallow(log, "set_avatar_from_url: uniquify")

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
            await _safe_disconnect(client)
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

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
    try:
        # Уникализация байтов аватара — чтобы флот не делил байт-в-байт одно фото
        # (кластеризация фермы). Fail-safe: при сбое остаются исходные байты.
        try:
            from services import media_uniquifier as _mu
            photo_bytes = _mu.uniquify(photo_bytes, filename=filename)
        except Exception:
            log_exc_swallow(log, "set_avatar_from_bytes: uniquify")
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
            await _safe_disconnect(client)
        except Exception as e:
            log_exc_swallow(log, "set_avatar_from_bytes: disconnect")


# ── Username ──────────────────────────────────────────────────────────────────

async def set_username(
    session_string: str,
    _acc: dict | None,
    username: str,
) -> dict[str, Any]:
    from telethon.tl.functions.account import UpdateUsernameRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
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

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log_exc_swallow(log, "set_2fa_password: disconnect")


# ── Безопасность: закрыть сторонние сессии ────────────────────────────────────

async def close_other_sessions(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Завершить ВСЕ прочие авторизации аккаунта (кроме текущей).
    Аналог «Закрыть сторонние сессии» — защита угнанных/прогретых аккаунтов."""
    from telethon.tl.functions.auth import ResetAuthorizationsRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
    try:
        await asyncio.wait_for(client(ResetAuthorizationsRequest()), timeout=_ACTION_TIMEOUT)
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await _safe_disconnect(client)
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
    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
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
    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log.warning('profile_setter: get_login_code disconnect failed: %s', e)


# ── Сброс/удаление полей профиля ──────────────────────────────────────────────

async def clear_bio(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Очистить bio (О себе). Аналог «Удалить bio» Telegram Expert."""
    from telethon.tl.functions.account import UpdateProfileRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log.warning('profile_setter: clear_bio disconnect failed: %s', e)


async def set_bio(session_string: str, _acc: dict | None, about: str) -> dict[str, Any]:
    """Установить ТОЛЬКО описание «О себе», не трогая имя и фамилию. Отдельная
    функция (не общий сеттер имени): тот при пустой фамилии затирал бы её.
    Описание обрезается до 70 символов (лимит Telegram)."""
    from telethon.tl.functions.account import UpdateProfileRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
    try:
        await asyncio.wait_for(
            client(UpdateProfileRequest(about=(about or "")[:70])),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        log.warning('profile_setter error: %s', exc)
        return {"ok": False, "error": str(exc)[:150]}
    finally:
        try:
            await _safe_disconnect(client)
        except Exception as e:
            log_exc_swallow(log, "set_bio: disconnect")


async def remove_username(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Снять @username аккаунта (пустая строка). Аналог «Удалить username»."""
    return await set_username(session_string, _acc, "")


async def remove_avatar(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Удалить ВСЕ фото профиля. Аналог «Удалить фото» Telegram Expert."""
    from telethon import utils
    from telethon.tl.functions.photos import DeletePhotosRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log.warning('profile_setter: remove_avatar disconnect failed: %s', e)


# ── Сброс 2FA (снять пароль) ──────────────────────────────────────────────────

async def reset_2fa(
    session_string: str, _acc: dict | None, current_password: str
) -> dict[str, Any]:
    """Снять 2FA-пароль (нужен текущий пароль). Аналог «Сбросить/удалить 2FA»."""
    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log.warning('profile_setter: reset_2fa disconnect failed: %s', e)


# ── Держать онлайн (разовый пинг) ─────────────────────────────────────────────

async def set_online(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Выставить статус «в сети» (offline=False). Разовый пинг присутствия.
    Непрерывный keep-online — задача планировщика (см. backlog)."""
    from telethon.tl.functions.account import UpdateStatusRequest

    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
        except Exception as e:
            log.warning('profile_setter: set_online disconnect failed: %s', e)


# ── Проверка ограничений (бан / restricted / deleted) ─────────────────────────

async def check_restriction(session_string: str, _acc: dict | None) -> dict[str, Any]:
    """Проверить состояние аккаунта: жив / ограничен / удалён.
    Аналог раздела «ПРОВЕРКА» Telegram Expert. Возвращает
    {ok, alive, restricted, deleted, reason, username, user_id}."""
    try:
        client = await _connect(session_string, _acc)
    except ConnectFailed as _cf:
        return {"ok": False, "error": str(_cf)}
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
            await _safe_disconnect(client)
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


# ── Набор аватаров ───────────────────────────────────────────────────────────

MAX_AVATAR_URLS = 50


def avatar_urls_of(params: dict) -> list[str]:
    """Все адреса аватаров операции: набор ``avatar_urls`` или один ``avatar_url``.

    Одна картинка на весь флот — это одинаковое лицо у сотни аккаунтов; даже с
    байтовой уникализацией человек видит один и тот же портрет. Поэтому экран
    принимает НАБОР, а старые операции в очереди (один адрес) читаются как
    набор из одного.
    """
    urls = [str(u).strip() for u in (params.get("avatar_urls") or []) if str(u).strip()]
    if not urls and str(params.get("avatar_url") or "").strip():
        urls = [str(params["avatar_url"]).strip()]
    return urls[:MAX_AVATAR_URLS]


def pick_avatar_url(params: dict, slot: int) -> str:
    """Аватар для аккаунта номер ``slot`` в прогоне — по кругу по набору.

    По кругу, а не случайно: при наборе из десяти картинок на десять аккаунтов
    случайный выбор почти наверняка дал бы двоим одно лицо, а круг — нет.
    """
    urls = avatar_urls_of(params)
    if not urls:
        return ""
    return urls[int(slot) % len(urls)]


# ── Предпросмотр оформления ──────────────────────────────────────────────────

_FIELD_LIMITS = {"first_name": 64, "last_name": 64, "about": 70, "username": 32}


def render_preview(fields: dict, draws: int = 8, show: int = 5) -> dict:
    """Как будут выглядеть профили — теми же функциями, что и исполнитель.

    Возвращает ``{samples, distinct, too_long}``: ``samples`` — до ``show``
    РАЗНЫХ профилей, ``distinct`` — сколько разных вышло из ``draws`` бросков
    (1 = у всех аккаунтов будет одно и то же), ``too_long`` — поля, которые
    Telegram обрежет (подпись по-русски). Чистая функция.
    """
    labels = {"first_name": "Имя", "last_name": "Фамилия", "about": "Описание",
              "username": "Юзернейм"}
    rolled: list[dict] = []
    too_long: set[str] = set()
    for _ in range(max(1, int(draws))):
        one = {}
        for key, limit in _FIELD_LIMITS.items():
            tpl = str(fields.get(key) or "")
            if not tpl:
                continue
            val = expand_spintax(tpl)
            if len(val) > limit:
                too_long.add(labels[key])
            one[key] = val[:limit]
        rolled.append(one)
    seen: list[dict] = []
    for r in rolled:
        if r not in seen:
            seen.append(r)
    return {
        "samples": seen[:show],
        "distinct": len(seen),
        "too_long": sorted(too_long),
    }


# ── Сборка параметров операции с экрана ──────────────────────────────────────

# Операции экрана «Оформление» → подпись по-русски (её видит владелец в
# очереди, в итогах и в подтверждении).
SETTER_OPS = {
    "name": "Имя и описание",
    "avatar": "Аватар",
    "2fa": "Пароль 2FA",
    "username": "Юзернейм",
    "privacy": "Приватность",
    "close_sessions": "Закрыть чужие сеансы",
    "remove_avatar": "Удалить фото",
    "clear_bio": "Очистить описание",
    "reset_2fa": "Снять 2FA",
}

PRIVACY_LABELS = {"phone": "номер телефона", "invite": "добавление в группы",
                  "lastseen": "время в сети"}

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def build_setter_params(op: str, body: dict) -> dict:
    """Проверить ввод экрана и собрать params операции (без account_ids).

    Ошибка ввода — ``ValueError`` с текстом для владельца. Чистая функция: та же
    проверка, что на сервере, тестируется без базы и сети.
    """
    if op not in SETTER_OPS:
        raise ValueError("Неизвестная операция оформления")
    params: dict = {"op": op}
    if op == "name":
        nd = {k: str(body.get(k) or "").strip()[:512]
              for k in ("first_name", "last_name", "about")}
        if not any(nd.values()):
            raise ValueError("Заполните хотя бы одно поле: имя, фамилию или описание")
        params["name_data"] = nd
        params["keep_empty"] = True
    elif op == "avatar":
        raw = body.get("avatar_urls")
        if isinstance(raw, str):
            raw = raw.splitlines()
        urls = [str(u).strip() for u in (raw or []) if str(u).strip()]
        one = str(body.get("avatar_url") or "").strip()
        if one and one not in urls:
            urls.insert(0, one)
        if not urls:
            raise ValueError("Добавьте хотя бы одну картинку")
        if len(urls) > MAX_AVATAR_URLS:
            raise ValueError(f"Не больше {MAX_AVATAR_URLS} картинок в наборе")
        bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
        if bad:
            raise ValueError("Каждая ссылка на картинку должна начинаться с http")
        params["avatar_urls"] = urls
    elif op == "2fa":
        new_pass = str(body.get("new_password") or "").strip()
        if len(new_pass) < 4:
            raise ValueError("Пароль — минимум 4 символа")
        params["new_password"] = new_pass[:64]
        params["current_password"] = str(body.get("current_password") or "").strip()[:64]
        params["hint"] = str(body.get("hint") or "").strip()[:64]
    elif op == "username":
        tpl = str(body.get("username") or "").strip().lstrip("@")
        if not tpl:
            raise ValueError("Укажите шаблон юзернейма")
        # Шаблон проверяем на броске: спинтакс и {rand} раскрываются у каждого
        # аккаунта, а заведомо негодная форма должна отбиться здесь, а не
        # сотней одинаковых ошибок в итогах операции.
        sample = render_username(tpl, 0)
        if not _USERNAME_RE.match(sample):
            raise ValueError("Юзернейм: латиница, цифры и «_», 5–32 символа, "
                             "начинается с буквы")
        params["username"] = tpl[:200]
    elif op == "privacy":
        key = str(body.get("privacy_key") or "").strip()
        if key not in PRIVACY_LABELS:
            raise ValueError("Выберите, что скрыть или открыть")
        params["privacy_key"] = key
        params["privacy_allow"] = bool(body.get("privacy_allow"))
    elif op == "reset_2fa":
        # Пустой пароль допустим: исполнитель возьмёт сохранённый у аккаунта.
        params["current_password"] = str(body.get("current_password") or "").strip()[:64]
    return params


def setter_label(op: str, params: dict, count: int) -> str:
    """Подпись операции в очереди: что делаем и на скольких аккаунтах."""
    what = SETTER_OPS.get(op, op)
    if op == "privacy":
        verb = "открыть" if params.get("privacy_allow") else "скрыть"
        what = f"Приватность: {verb} {PRIVACY_LABELS.get(params.get('privacy_key'), '')}"
    elif op == "avatar":
        n = len(avatar_urls_of(params))
        if n > 1:
            what = f"Аватар (набор из {n})"
    return f"Оформление: {what} × {count} акк."


def render_username(template: str, slot: int) -> str:
    """Юзернейм для аккаунта: спинтакс + ``{rand}`` (4 случайные цифры) +
    ``{n}`` (номер аккаунта в прогоне). Без ``{rand}``/``{n}``/спинтакса весь
    флот получил бы один и тот же юзернейм, а он уникален в Telegram — второй
    аккаунт получил бы отказ «занято»."""
    import random as _r

    out = expand_spintax(template or "")
    out = out.replace("{n}", str(int(slot) + 1))
    while "{rand}" in out:
        out = out.replace("{rand}", f"{_r.randint(0, 9999):04d}", 1)
    return out.strip().lstrip("@")


def name_bio_kwargs(name_data: dict, params: dict) -> dict:
    """Аргументы ``set_name_bio`` по шаблонам — спинтакс раскрыт для аккаунта.

    ``keep_empty`` (так шлёт экран «Оформление»): пустое поле значит «не
    трогать», а не «стереть». Без этого владелец, менявший только описание,
    терял фамилии всего флота: ``UpdateProfileRequest(last_name="")`` её
    стирает. Старые операции и карточка аккаунта (без флага) ведут себя
    по-прежнему — пустое поле очищается.
    """
    keep = bool(params.get("keep_empty"))
    out: dict = {"first_name": expand_spintax(name_data.get("first_name", "") or "")}
    for key in ("last_name", "about"):
        tpl = name_data.get(key, "") or ""
        if keep and not tpl.strip():
            out[key] = None
        else:
            out[key] = expand_spintax(tpl)
    return out


async def apply_op(session_string: str, acc: dict, op: str, params: dict,
                   slot: int = 0) -> dict:
    """Единый диспетчер аккаунт-операции (op → движковая функция).

    ОДНА реализация для очереди (op_worker._exec_bulk_set_profile) и инлайн-пути
    (mini_app account_profile) — без дублей. Возвращает {ok, error, ...}.
    ``slot`` — номер аккаунта в прогоне: по нему выбирается аватар из набора и
    подставляется ``{n}`` в юзернейм.
    """
    if op == "name":
        nd = params.get("name_data", {})
        return await set_name_bio(session_string, acc, **name_bio_kwargs(nd, params))
    if op == "avatar":
        return await set_avatar_from_url(session_string, acc, pick_avatar_url(params, slot))
    if op == "2fa":
        return await set_2fa_password(
            session_string, acc,
            new_password=params.get("new_password", ""),
            current_password=params.get("current_password", ""),
            hint=params.get("hint", ""),
        )
    if op == "username":
        return await set_username(session_string, acc,
                                  render_username(params.get("username", ""), slot))
    if op == "close_sessions":
        return await close_other_sessions(session_string, acc)
    if op == "privacy":
        return await set_privacy(
            session_string, acc,
            key=params.get("privacy_key", "phone"),
            allow=bool(params.get("privacy_allow", False)),
        )
    if op == "bio":
        return await set_bio(session_string, acc, expand_spintax(params.get("about", "")))
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
    if op == "login_code":
        return await get_login_code(session_string, acc)
    return {"ok": False, "error": f"unknown op: {op}"}


async def _safe_disconnect(client):
    """Закрытие сокета с потолком — зависший teardown не морозит операцию.
    Делегирует account_manager.safe_disconnect (ленивый импорт: без цикла)."""
    from services.account_manager import safe_disconnect
    await safe_disconnect(client)
