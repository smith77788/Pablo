"""Движок репортинга — массовые жалобы на пользователей, каналы, сообщения.

Использует ReportPeerRequest (на профиль/канал) и ReportRequest (на сообщения).
Каждый аккаунт отправляет одну жалобу с небольшой паузой.

Надёжность (жалоба владельца «операция периодически падает»): отправка жалобы —
это цепочка connect → разрешить цель → вызвать report, и падение ЛЮБОГО звена
раньше считалось провалом аккаунта. Большинство таких падений транзиентны:
кратковременный сетевой сбой на подключении, промах кэша при разрешении
@username (его чинит полное разрешение), короткий FloodWait. Поэтому здесь:
  * подключение — с одним повтором на транзиентном сбое;
  * разрешение цели — через get_input_entity (часто из кэша, без запроса), а на
    промахе — полное get_entity (резолвит не виденные раньше @username/ссылки/ID);
  * короткий FloodWait пережидаем на месте и повторяем ОДИН раз; длинный —
    отдаём наверх (flood_wait), чтобы исполнитель поставил cooldown, а не держал
    арендованный аккаунт подключённым без единого запроса.
Остаётся честным: если звено упало окончательно, аккаунт отдаёт ok=False с
причиной — исход операции по-прежнему считается по факту (см. op_worker).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from telethon.errors import FloodWaitError

from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 15.0
_ACTION_TIMEOUT = 12.0

# Короткий FloodWait дешевле переждать на месте и повторить; длинный держать
# нельзя — арендованный аккаунт висел бы подключённым без запросов, ровно то
# поведение, от которого Telegram отзывает сессию. Порог намеренно маленький:
# межцелевые паузы операции и так секундные.
_MAX_FLOOD_INLINE = 25

REPORT_REASONS = {
    "spam":       ("🗑 Спам", "InputReportReasonSpam"),
    "violence":   ("⚔️ Насилие", "InputReportReasonViolence"),
    "porn":       ("🔞 Порнография", "InputReportReasonPornography"),
    "childabuse": ("🚨 Насилие над детьми", "InputReportReasonChildAbuse"),
    "drugs":      ("💊 Наркотики", "InputReportReasonIllegalDrugs"),
    "fake":       ("🎭 Фейк", "InputReportReasonFake"),
    "personal":   ("🔓 Личные данные", "InputReportReasonPersonalDetails"),
    "other":      ("📋 Другое", "InputReportReasonOther"),
}


def _get_reason(reason_key: str):
    from telethon.tl.types import (
        InputReportReasonSpam, InputReportReasonViolence, InputReportReasonPornography,
        InputReportReasonChildAbuse,
        InputReportReasonIllegalDrugs, InputReportReasonFake, InputReportReasonPersonalDetails,
        InputReportReasonOther,
    )
    mapping = {
        "spam":       InputReportReasonSpam(),
        "violence":   InputReportReasonViolence(),
        "porn":       InputReportReasonPornography(),
        "childabuse": InputReportReasonChildAbuse(),
        "drugs":      InputReportReasonIllegalDrugs(),
        "fake":       InputReportReasonFake(),
        "personal":   InputReportReasonPersonalDetails(),
        "other":      InputReportReasonOther(),
    }
    return mapping.get(reason_key, InputReportReasonSpam())


def _is_transient(exc: BaseException) -> bool:
    """Сбой, который имеет смысл повторить (сеть/таймаут), а не отказ по сути."""
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))


async def _connect(client) -> None:
    """Подключить клиент с одним повтором на транзиентном сбое.

    Первый коннект часто обрывается на холодном прокси/релее; слепой провал на
    нём помечал аккаунт неудачником, хотя второй заход проходит.
    """
    last: BaseException | None = None
    for attempt in (1, 2):
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
            return
        except FloodWaitError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 2 or not _is_transient(exc):
                raise
            await asyncio.sleep(1.5)
    if last is not None:
        raise last


async def _resolve_peer(client, target_ref):
    """Устойчиво разрешить цель жалобы в сущность/InputPeer.

    get_input_entity отвечает из кэша без сетевого запроса, когда аккаунт цель
    уже видел; на промахе уходим в полное get_entity, которое РЕЗОЛВИТ не
    виденные раньше @username/ссылки по ResolveUsername. Раньше звали только
    get_entity — лишний сетевой запрос на каждую жалобу и лишняя точка отказа
    там, где кэш уже знает цель. FloodWait пробрасываем — его переждёт верх.
    """
    try:
        return await asyncio.wait_for(
            client.get_input_entity(target_ref), timeout=_ACTION_TIMEOUT)
    except FloodWaitError:
        raise
    except Exception:
        return await asyncio.wait_for(
            client.get_entity(target_ref), timeout=_ACTION_TIMEOUT)


async def _report_call(client, request):
    """Вызвать report-запрос, пережидая КОРОТКИЙ FloodWait один раз.

    Длинный FloodWait пробрасываем наверх — его нельзя пережидать на живом
    арендованном коннекте.
    """
    for attempt in (1, 2):
        try:
            return await asyncio.wait_for(client(request), timeout=_ACTION_TIMEOUT)
        except FloodWaitError as fw:
            secs = int(getattr(fw, "seconds", 0) or 0)
            if attempt == 1 and 0 < secs <= _MAX_FLOOD_INLINE:
                await asyncio.sleep(secs + 1)
                continue
            raise


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
        await _connect(client)
        peer = await _resolve_peer(client, target_ref)
        reason = _get_reason(reason_key)
        await _report_call(
            client, ReportPeerRequest(peer=peer, reason=reason, message=message_text[:512]))
        return {"ok": True, "error": None}
    except FloodWaitError as fw:
        secs = int(getattr(fw, "seconds", 0) or 0)
        log.debug("report_peer target=%s flood_wait=%ss", target_ref, secs)
        return {"ok": False, "error": f"Telegram попросил подождать {secs}с",
                "flood_wait": secs, "retryable": True}
    except Exception as exc:
        err = str(exc)[:150]
        log.debug("report_peer target=%s error: %s", target_ref, err)
        return {"ok": False, "error": err, "retryable": _is_transient(exc)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "report_peer: disconnect")


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
        await _connect(client)
        peer = await _resolve_peer(client, channel_ref)
        reason = _get_reason(reason_key)
        await _report_call(
            client, ReportRequest(peer=peer, id=msg_ids, reason=reason, message=message_text[:512]))
        return {"ok": True, "error": None}
    except FloodWaitError as fw:
        secs = int(getattr(fw, "seconds", 0) or 0)
        log.debug("report_message channel=%s flood_wait=%ss", channel_ref, secs)
        return {"ok": False, "error": f"Telegram попросил подождать {secs}с",
                "flood_wait": secs, "retryable": True}
    except Exception as exc:
        err = str(exc)[:150]
        log.debug("report_message channel=%s error: %s", channel_ref, err)
        return {"ok": False, "error": err, "retryable": _is_transient(exc)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "report_message: disconnect")


def parse_target_ref(text: str) -> str:
    """Нормализует ссылку/username цели → @username, числовой ID, или
    канонический https://t.me/+HASH для приватных invite-ссылок.

    Delegates to account_manager.normalize_telegram_join_ref — the previous
    regex here matched "t.me/joinchat/HASH" as if "joinchat" were a public
    username, and didn't match "t.me/+HASH" at all, silently corrupting
    private invite links.
    """
    text = text.strip()
    if re.match(r"^-?\d+$", text):
        return text  # numeric chat ID — not a join ref, pass through as-is

    from services.account_manager import format_telegram_join_ref_display

    formatted = format_telegram_join_ref_display(text)
    return formatted or text
