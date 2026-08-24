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
    import asyncio
    client = None
    try:
        device = {"proxy_url": proxy_url} if proxy_url else None
        client = _make_client(session_string, device)
        await asyncio.wait_for(client.connect(), timeout=15)
        me = await client.get_me()
        return {
            "valid": True,
            "phone": me.phone or "",
            "user_id": me.id,
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}
    finally:
        if client is not None and client.is_connected():
            await client.disconnect()


async def import_sessions(
    pool: asyncpg.Pool,
    owner_id: int,
    raw_data: str,
    proxy_url: str | None = None,
    proxy_id: int | None = None,
) -> dict:
    # proxy_url — через что ПРОВЕРЯЕМ сессию. proxy_id (если задан) — реальный прокси
    # владельца, который СРАЗУ закрепляем за импортированным аккаунтом (изоляция с
    # первого шага: иначе аккаунт падает на общий CF-relay с единым IP). proxy_id
    # доверяем только свой — сверяем принадлежность владельцу.
    if proxy_id is not None:
        try:
            owns_proxy = await pool.fetchval(
                "SELECT 1 FROM user_proxies WHERE id=$1 AND owner_id=$2", proxy_id, owner_id)
            if not owns_proxy:
                proxy_id = None  # чужой/несуществующий прокси не закрепляем
        except Exception:
            proxy_id = None
    lines = [l.strip() for l in raw_data.strip().splitlines() if l.strip()]
    if not lines:
        return {"imported": 0, "failed": 0, "errors": ["Пустые данные"]}
    imported = 0
    failed = 0
    errors = []
    # Каждая строка валидируется реальным подключением к Telegram (до 15с) —
    # синхронно. Ограничиваем порцию, чтобы запрос не завис молча по тайм-ауту;
    # честно сообщаем про остаток. Массовый импорт (файлы) — через бота.
    MAX_PER_IMPORT = 20
    truncated = 0
    if len(lines) > MAX_PER_IMPORT:
        truncated = len(lines) - MAX_PER_IMPORT
        lines = lines[:MAX_PER_IMPORT]
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
            # Классифицируем причину, а не сваливаем всё в «невалидная сессия»:
            # мёртвый прокси/сеть/флуд → сессия скорее всего ЦЕЛА, дело в прокси —
            # иначе пользователь удалит рабочую сессию. (net/flood отделяем от
            # реально недействительной сессии.)
            from services.contacts_hub.sync_service import classify_session_error
            friendly, _kind = classify_session_error(result.get('error', '') or '')
            errors.append(f"Строка {i+1}: {friendly}")
            continue
        # Дедуп по детерминированному fingerprint (шифр недетерминирован, поэтому
        # сравнение по session_str=шифротекст не сработало бы). Fallback на
        # плейнтекст-равенство ловит legacy-строки, у которых session_fp ещё NULL.
        from services.token_vault import encrypt_token, session_fingerprint

        _fp = session_fingerprint(session_str)
        existing = await pool.fetchrow(
            "SELECT id, owner_id FROM tg_accounts WHERE session_fp=$1 OR session_str=$2",
            _fp, session_str,
        )
        if existing:
            failed += 1
            # Дедуп глобальный (одна Telegram-сессия = один аккаунт на платформе),
            # но id раскрываем ТОЛЬКО владельцу. Иначе импорт чужой сессии выдал бы
            # внутренний id аккаунта другого владельца (cross-tenant disclosure).
            if existing["owner_id"] == owner_id:
                errors.append(f"Строка {i+1}: сессия уже есть в вашем аккаунте (id={existing['id']})")
            else:
                errors.append(f"Строка {i+1}: сессия уже используется на платформе")
            continue
        # Уникальный device-отпечаток НА АККАУНТ. Иначе импортные сессии идут в
        # БД без device_* и на _make_client получают ОДИН дефолтный отпечаток
        # (Samsung SM-S911B / en-US) — массовая коллизия фингерпринтов на самом
        # частом пути (покупные сессии). locale выводится из страны номера.
        from services.account_manager import (
            generate_device_fingerprint,
            country_code_from_phone,
        )

        phone = result.get('phone', '') or ''
        dev = generate_device_fingerprint(country_code_from_phone(phone))
        try:
            await pool.execute(
                """INSERT INTO tg_accounts
                       (owner_id, session_str, session_fp, phone, is_active, acc_status,
                        device_model, system_version, app_version, lang_code, system_lang_code,
                        proxy_id)
                   VALUES ($1, $2, $3, $4, TRUE, 'active',
                        $5, $6, $7, $8, $9, $10)""",
                owner_id, encrypt_token(session_str), _fp, phone,
                dev["device_model"], dev["system_version"], dev["app_version"],
                dev["lang_code"], dev["system_lang_code"], proxy_id,
            )
            imported += 1
        except Exception as e:
            failed += 1
            errors.append(f"Строка {i+1}: ошибка БД — {str(e)[:100]}")
    # CF-релей НЕ раздаём импортированным аккаунтам автоматически: без прокси —
    # прямой host-IP. Прокси задаёт пользователь (proxy_id при импорте), CF/IPv6 —
    # только по явному выбору. Авто-CF делала релей «основой» и запирала аккаунты.
    if truncated:
        # ВПЕРЁД: иначе срез errors[:20] обрезал бы это важное сообщение, когда
        # набралось 20 построчных ошибок.
        errors.insert(0,
            f"Ещё {truncated} строк не обработано за раз — импортируйте порцией "
            f"до {MAX_PER_IMPORT} или загрузите файл сессий в боте.")
    return {"imported": imported, "failed": failed, "errors": errors[:20]}
