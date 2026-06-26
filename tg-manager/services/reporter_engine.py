"""Движок репортинга — массовые жалобы на пользователей, каналы, сообщения.

Использует ReportPeerRequest (на профиль/канал) и ReportRequest (на сообщения).
Каждый аккаунт отправляет одну жалобу с небольшой паузой.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 12.0

REPORT_REASONS = {
    "spam":     ("🗑 Спам", "InputReportReasonSpam"),
    "violence": ("⚔️ Насилие", "InputReportReasonViolence"),
    "porn":     ("🔞 Порнография", "InputReportReasonPornography"),
    "drugs":    ("💊 Наркотики", "InputReportReasonIllegalDrugs"),
    "fake":     ("🎭 Фейк", "InputReportReasonFake"),
    "personal": ("🔓 Личные данные", "InputReportReasonPersonalDetails"),
    "other":    ("📋 Другое", "InputReportReasonOther"),
}


def _get_reason(reason_key: str):
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence, InputReportReasonPornography,
        InputReportReasonIllegalDrugs, InputReportReasonFake, InputReportReasonPersonalDetails,
        InputReportReasonOther,
    )
    mapping = {
        "spam":     InputReportReasonSpam(),
        "violence": InputReportReasonViolence(),
        "porn":     InputReportReasonPornography(),
        "drugs":    InputReportReasonIllegalDrugs(),
        "fake":     InputReportReasonFake(),
        "personal": InputReportReasonPersonalDetails(),
        "other":    InputReportReasonOther(),
    }
    return mapping.get(reason_key, InputReportReasonSpam())


async def report_peer(
    session_string: str,
    _acc: dict | None,
    target_ref: str,
    reason_key: str,
    message_text: str = "",
) -> dict[str, Any]:
    """Пожаловаться на пользователя или канал (ReportPeerRequest)."""
    from services.account_manager import _make_client
    from telethon.tl.functions.account import ReportPeerRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        peer = await asyncio.wait_for(client.get_entity(target_ref), timeout=_ACTION_TIMEOUT)
        reason = _get_reason(reason_key)
        await asyncio.wait_for(
            client(ReportPeerRequest(peer=peer, reason=reason, message=message_text[:512])),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        err = str(exc)[:150]
        log.debug("report_peer target=%s error: %s", target_ref, err)
        return {"ok": False, "error": err}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def report_message(
    session_string: str,
    _acc: dict | None,
    channel_ref: str,
    msg_ids: list[int],
    reason_key: str,
    message_text: str = "",
) -> dict[str, Any]:
    """Пожаловаться на конкретные сообщения в канале/группе (ReportRequest)."""
    from services.account_manager import _make_client
    from telethon.tl.functions.messages import ReportRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        peer = await asyncio.wait_for(client.get_entity(channel_ref), timeout=_ACTION_TIMEOUT)
        reason = _get_reason(reason_key)
        await asyncio.wait_for(
            client(ReportRequest(peer=peer, id=msg_ids, reason=reason, message=message_text[:512])),
            timeout=_ACTION_TIMEOUT,
        )
        return {"ok": True, "error": None}
    except Exception as exc:
        err = str(exc)[:150]
        log.debug("report_message channel=%s error: %s", channel_ref, err)
        return {"ok": False, "error": err}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def parse_target_ref(text: str) -> str:
    text = text.strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]{3,})", text)
    if m:
        return f"@{m.group(1)}"
    if text.startswith("@") or re.match(r"^-?\d+$", text):
        return text
    if re.match(r"^[A-Za-z0-9_]{3,}$", text):
        return f"@{text}"
    return text
