from __future__ import annotations
import os
import json
import logging
import re
from pathlib import Path
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


SESSION_FORMATS = {
    "string_session": re.compile(r'^[A-Za-z0-9+/=]{100,}$'),
    "pyrogram_json": re.compile(r'^\{.*"dc_id".*"api_id".*"test_mode".*\}$', re.DOTALL),
    "tdata_dir": re.compile(r'^[a-f0-9]{32}$'),
}


def detect_format(data: str) -> str:
    data = data.strip()
    if data.startswith('{'):
        try:
            parsed = json.loads(data)
            if 'dc_id' in parsed and 'api_id' in parsed:
                return 'pyrogram_json'
        except json.JSONDecodeError:
            pass
    if SESSION_FORMATS['string_session'].match(data):
        return 'string_session'
    if len(data) == 32 and all(c in '0123456789abcdef' for c in data):
        return 'tdata_hash'
    return 'unknown'


def extract_session_string(data: str, fmt: str) -> str | None:
    if fmt == 'string_session':
        return data.strip()
    if fmt == 'pyrogram_json':
        try:
            parsed = json.loads(data.strip())
            return parsed.get('session')
        except Exception:
            return None
    return None


async def validate_session(session_string: str, proxy_url: str | None = None) -> dict:
    from services.account_manager import _make_client
    try:
        client = _make_client(session_string)
        import asyncio
        await asyncio.wait_for(client.connect(), timeout=15)
        me = await client.get_me()
        await client.disconnect()
        return {
            "valid": True,
            "phone": me.phone or "",
            "user_id": me.id,
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


async def import_sessions(
    pool: asyncpg.Pool,
    owner_id: int,
    raw_data: str,
    proxy_url: str | None = None,
) -> dict:
    lines = [l.strip() for l in raw_data.strip().splitlines() if l.strip()]
    if not lines:
        return {"imported": 0, "failed": 0, "errors": ["Пустые данные"]}
    imported = 0
    failed = 0
    errors = []
    for i, line in enumerate(lines):
        fmt = detect_format(line)
        if fmt == 'unknown':
            failed += 1
            errors.append(f"Строка {i+1}: неизвестный формат")
            continue
        session_str = extract_session_string(line, fmt)
        if not session_str:
            failed += 1
            errors.append(f"Строка {i+1}: не удалось извлечь сессию")
            continue
        result = await validate_session(session_str, proxy_url)
        if not result['valid']:
            failed += 1
            errors.append(f"Строка {i+1}: невалидная сессия — {result.get('error', '?')}")
            continue
        existing = await pool.fetchrow(
            "SELECT id FROM tg_accounts WHERE session_str=$1", session_str
        )
        if existing:
            failed += 1
            errors.append(f"Строка {i+1}: сессия уже существует (id={existing['id']})")
            continue
        try:
            await pool.execute(
                """INSERT INTO tg_accounts (owner_id, session_str, phone, is_active, acc_status)
                   VALUES ($1, $2, $3, TRUE, 'active')""",
                owner_id, session_str, result.get('phone', ''),
            )
            imported += 1
        except Exception as e:
            failed += 1
            errors.append(f"Строка {i+1}: ошибка БД — {str(e)[:100]}")
    return {"imported": imported, "failed": failed, "errors": errors[:20]}
