"""
Session Converter — конвертация форматов сессий.

Поддерживаемые форматы:
- Pyrogram JSON → Telethon StringSession
- SQLite session → Telethon StringSession (через telethon.sync или asyncio)
- tdata — определение (конвертация через opentele если установлена)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import tempfile
from typing import Optional

log = logging.getLogger(__name__)


class ConversionError(Exception):
    pass


async def pyrogram_json_to_telethon(json_str: str) -> tuple[str, dict]:
    """
    Конвертирует Pyrogram JSON сессию в Telethon StringSession.

    Pyrogram JSON format:
    {"dc_id": 1, "api_id": 123, "test_mode": false,
     "auth_key": "base64...", "date": 0, "user_id": 123, "is_bot": false}

    Returns (session_string, info_dict)
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ConversionError(f"Некорректный JSON: {e}")

    required = ["dc_id", "auth_key", "user_id"]
    for key in required:
        if key not in data:
            raise ConversionError(f"Отсутствует поле: {key}")

    try:
        dc_id = int(data["dc_id"])
        auth_key = base64.b64decode(data["auth_key"])
        user_id = int(data["user_id"])
    except Exception as e:
        raise ConversionError(f"Ошибка парсинга данных: {e}")

    if len(auth_key) != 256:
        raise ConversionError(f"auth_key должен быть 256 байт, получено {len(auth_key)}")

    # Telethon StringSession format:
    # Version(1) + DC ID(1) + IP bytes(4 or 16) + Port(2) + Auth Key(256)
    # IP и port берём из стандартных DC-адресов Telegram
    DC_IPS = {
        1: "149.154.175.53",
        2: "149.154.167.51",
        3: "149.154.175.100",
        4: "149.154.167.91",
        5: "91.108.56.130",
    }

    dc_ip = DC_IPS.get(dc_id, "149.154.167.51")
    ip_parts = [int(x) for x in dc_ip.split(".")]
    ip_bytes = bytes(ip_parts)
    port = 443

    # Pack: version(1) + dc_id(1) + ip(4) + port(2) + auth_key(256)
    session_bytes = struct.pack(">BBHH", 1, dc_id, 0, port)  # simplified packing
    # Correct Telethon format
    data_bytes = struct.pack(">B", dc_id) + ip_bytes + struct.pack(">H", port) + auth_key
    # Telethon StringSession = base64url(version_byte + data)
    version = b"\x01"
    session_string = base64.urlsafe_b64encode(version + data_bytes).decode()
    # Strip padding
    session_string = session_string.rstrip("=")

    info = {
        "dc_id": dc_id,
        "user_id": user_id,
        "is_bot": bool(data.get("is_bot", False)),
        "format": "pyrogram_json",
    }

    log.info("session_converter: pyrogram_json → telethon dc=%d user_id=%d", dc_id, user_id)
    return session_string, info


async def sqlite_to_telethon(sqlite_path: str) -> tuple[str, dict]:
    """
    Конвертирует SQLite session файл (TDLib/Telethon format) в Telethon StringSession.
    """
    import sqlite3

    if not os.path.exists(sqlite_path):
        raise ConversionError(f"Файл не найден: {sqlite_path}")

    try:
        conn = sqlite3.connect(sqlite_path)
        try:
            # Telethon-style SQLite session
            cursor = conn.execute("SELECT * FROM sessions")
            row = cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                session_data = dict(zip(cols, row))

                dc_id = session_data.get("dc_id", 2)
                auth_key_bytes = session_data.get("auth_key")

                if auth_key_bytes and len(auth_key_bytes) == 256:
                    DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51",
                              3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
                    dc_ip = DC_IPS.get(dc_id, "149.154.167.51")
                    ip_bytes = bytes(int(x) for x in dc_ip.split("."))
                    port = session_data.get("port", 443)
                    data_bytes = struct.pack(">B", dc_id) + ip_bytes + struct.pack(">H", port) + auth_key_bytes
                    session_string = base64.urlsafe_b64encode(b"\x01" + data_bytes).decode().rstrip("=")

                    info = {"dc_id": dc_id, "format": "sqlite_telethon"}
                    return session_string, info
        finally:
            conn.close()
    except Exception as e:
        raise ConversionError(f"Ошибка чтения SQLite: {e}")

    raise ConversionError("Не удалось извлечь сессию из SQLite файла")


def detect_tdata(path: str) -> dict:
    """
    Определяет является ли путь tdata-директорией Telegram Desktop.
    Возвращает {'is_tdata': bool, 'can_convert': bool, 'message': str}
    """
    if not os.path.isdir(path):
        return {"is_tdata": False, "can_convert": False, "message": "Не является директорией"}

    # tdata признаки: наличие файлов key_data, D877F783D5D3EF8C, settings/0
    indicators = ["key_data", "D877F783D5D3EF8C", "settings"]
    found = []
    for ind in indicators:
        if os.path.exists(os.path.join(path, ind)):
            found.append(ind)

    if len(found) >= 1:
        # Проверяем наличие opentele
        try:
            import opentele  # type: ignore
            can_convert = True
            msg = f"tdata обнаружен ({', '.join(found)}). opentele доступен — конвертация возможна."
        except ImportError:
            can_convert = False
            msg = f"tdata обнаружен ({', '.join(found)}). Установите opentele для конвертации: pip install opentele"

        return {"is_tdata": True, "can_convert": can_convert, "message": msg}

    return {"is_tdata": False, "can_convert": False, "message": "tdata не обнаружен"}


async def convert_auto(content: str | bytes, hint: str = "") -> tuple[str, dict]:
    """
    Автоматически определяет формат и конвертирует в Telethon StringSession.
    hint: 'pyrogram_json' | 'sqlite' | ''
    """
    if isinstance(content, bytes):
        # Попробуем как строку
        try:
            content = content.decode("utf-8")
        except Exception:
            raise ConversionError("Не удалось декодировать содержимое файла")

    # Попытка 1: Pyrogram JSON
    if hint == "pyrogram_json" or content.strip().startswith("{"):
        try:
            return await pyrogram_json_to_telethon(content)
        except ConversionError:
            pass

    # Попытка 2: Уже является Telethon StringSession
    if len(content.strip()) > 100 and not content.strip().startswith("{"):
        possible_session = content.strip()
        # Проверяем что это base64url
        try:
            decoded = base64.urlsafe_b64decode(possible_session + "==")
            if len(decoded) > 260:  # версия + DC + IP + port + auth_key
                return possible_session, {"format": "telethon_string", "dc_id": decoded[0] if decoded else 0}
        except Exception:
            pass

    raise ConversionError("Не удалось определить формат сессии. Поддерживаются: Pyrogram JSON, Telethon StringSession")
