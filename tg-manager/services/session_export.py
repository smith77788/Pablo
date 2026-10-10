"""Генератор Pyrogram JSON из Telethon-сессии аккаунта (экспорт).

Паритет с эталоном (Telegram Expert «Генератор Json»): обратная операция к
account_manager.import_from_pyrogram_json — из хранимой Telethon StringSession
собираем Pyrogram-совместимый JSON (dc_id, auth_key base64, user_id и т.д.),
пригодный для переноса аккаунта в другой софт.

build_pyrogram_json — чистая функция (без сети/БД/telethon), тестируется юнитом.
Формат auth_key — base64 (симметрично импортёру, который делает base64.b64decode).
"""
from __future__ import annotations

import base64
import json
import logging

log = logging.getLogger(__name__)


class SessionExportError(ValueError):
    pass


def build_pyrogram_json(
    dc_id: int,
    auth_key: bytes,
    api_id: int,
    user_id: int = 0,
    is_bot: bool = False,
    test_mode: bool = False,
) -> str:
    """Собрать Pyrogram JSON из компонентов сессии. Чистая, тестируемая."""
    if not isinstance(auth_key, (bytes, bytearray)) or len(auth_key) != 256:
        raise SessionExportError(
            f"auth_key должен быть 256 байт, получено {len(auth_key) if auth_key else 0}")
    if not dc_id or int(dc_id) <= 0:
        raise SessionExportError("некорректный dc_id")
    data = {
        "dc_id": int(dc_id),
        "api_id": int(api_id) if api_id else 0,
        "test_mode": bool(test_mode),
        "auth_key": base64.b64encode(bytes(auth_key)).decode("ascii"),
        "date": 0,
        "user_id": int(user_id or 0),
        "is_bot": bool(is_bot),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def session_to_pyrogram_json(
    session_string: str, api_id: int, user_id: int = 0, is_bot: bool = False
) -> str:
    """Декодировать Telethon StringSession (расшифровав vault) и собрать Pyrogram JSON.

    Тонкая обёртка над build_pyrogram_json: telethon-декод здесь, чистая сборка —
    в билдере (его и тестируем).
    """
    from services.token_vault import decrypt_token
    from telethon.sessions import StringSession

    raw = decrypt_token(session_string or "")
    if not raw or len(raw.strip()) < 10:
        raise SessionExportError("пустая или недоступная сессия")
    ss = StringSession(raw)
    dc_id = getattr(ss, "dc_id", None) or getattr(ss, "_dc_id", None)
    ak = getattr(ss, "auth_key", None)
    key = getattr(ak, "key", None) if ak is not None else None
    if not dc_id or not key:
        raise SessionExportError("не удалось извлечь dc_id/auth_key из сессии")
    return build_pyrogram_json(int(dc_id), bytes(key), api_id, user_id=user_id, is_bot=is_bot)
