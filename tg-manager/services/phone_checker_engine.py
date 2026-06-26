"""Чекер номеров телефонов.

Проверяет, зарегистрирован ли номер в Telegram.
Использует ImportContactsRequest → смотрит user_id → DeleteContacts.

Возвращает: {phone: str, registered: bool, user_id: int|None, username: str|None,
             first_name: str|None, premium: bool}
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 15.0
_BATCH_SIZE = 25  # Telegram позволяет до 25 контактов за раз


def normalize_phone(phone: str) -> str:
    """Нормализовать номер: удалить пробелы, скобки, тире."""
    phone = re.sub(r"[^\d+]", "", phone.strip())
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return phone


def parse_phone_list(text: str) -> list[str]:
    """Парсить список номеров из текста."""
    phones: list[str] = []
    for line in re.split(r"[,;\n]+", text):
        p = normalize_phone(line.strip())
        if len(p) >= 10:
            phones.append(p)
    return list(dict.fromkeys(phones))[:5000]


async def check_phones_batch(
    session_string: str,
    _acc: dict | None,
    phones: list[str],
) -> list[dict[str, Any]]:
    """Проверить пачку номеров. Вернуть список результатов."""
    from services.account_manager import _make_client
    from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
    from telethon.tl.types import InputPhoneContact

    client = _make_client(session_string, _acc)
    results: list[dict[str, Any]] = []

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        contacts = [
            InputPhoneContact(client_id=i, phone=p, first_name="chk", last_name="")
            for i, p in enumerate(phones)
        ]
        imported = await asyncio.wait_for(
            client(ImportContactsRequest(contacts=contacts)),
            timeout=_ACTION_TIMEOUT,
        )

        # Строим map phone → user
        phone_to_user: dict[str, Any] = {}
        for user in imported.users:
            raw_phone = getattr(user, "phone", "") or ""
            if raw_phone:
                p = normalize_phone(raw_phone)
                phone_to_user[p] = user

        for phone in phones:
            user = phone_to_user.get(phone)
            if user:
                results.append({
                    "phone": phone,
                    "registered": True,
                    "user_id": int(user.id),
                    "username": getattr(user, "username", "") or "",
                    "first_name": getattr(user, "first_name", "") or "",
                    "last_name": getattr(user, "last_name", "") or "",
                    "premium": bool(getattr(user, "premium", False)),
                })
            else:
                results.append({
                    "phone": phone,
                    "registered": False,
                    "user_id": None,
                    "username": None,
                    "first_name": None,
                    "last_name": None,
                    "premium": False,
                })

        # Чистим импортированные контакты
        if imported.users:
            try:
                await asyncio.wait_for(
                    client(DeleteContactsRequest(id=list(imported.users))),
                    timeout=_ACTION_TIMEOUT,
                )
            except Exception:
                pass

    except Exception as exc:
        log.warning("check_phones_batch error: %s", exc)
        for phone in phones:
            results.append({
                "phone": phone,
                "registered": None,  # None = ошибка проверки
                "user_id": None,
                "username": None,
                "first_name": None,
                "last_name": None,
                "premium": False,
                "error": str(exc)[:100],
            })
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    return results


def results_to_csv(results: list[dict]) -> bytes:
    """Конвертировать результаты в CSV bytes."""
    import io
    import csv

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["phone", "registered", "user_id", "username", "first_name", "last_name", "premium"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(results)
    return buf.getvalue().encode("utf-8-sig")
