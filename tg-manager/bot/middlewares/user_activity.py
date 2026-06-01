from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from services.logger import set_correlation_id

log = logging.getLogger(__name__)

_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{48,}\b")


def _safe_preview(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    redacted = _BOT_TOKEN_RE.sub("<bot_token>", value)
    redacted = _LONG_SECRET_RE.sub("<secret>", redacted)
    return redacted[:limit]


def _user_extra(event: TelegramObject) -> dict[str, Any]:
    user = getattr(event, "from_user", None)
    if not user:
        return {}
    return {
        "telegram_user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "language_code": user.language_code,
    }


class UserActivityLogMiddleware(BaseMiddleware):
    """Log manager-bot messages and callbacks with user, state, and duration."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        extra = _user_extra(event)
        user_id = extra.get("telegram_user_id")
        if user_id:
            set_correlation_id(user_id=user_id)

        if isinstance(event, CallbackQuery):
            extra.update(
                {
                    "event_kind": "callback",
                    "callback_data": _safe_preview(event.data),
                    "message_id": event.message.message_id if event.message else None,
                }
            )
        elif isinstance(event, Message):
            extra.update(
                {
                    "event_kind": "message",
                    "message_id": event.message_id,
                    "content_type": str(event.content_type),
                    "text_preview": _safe_preview(event.text or event.caption),
                }
            )
        else:
            extra["event_kind"] = type(event).__name__

        fsm_state = data.get("state")
        if fsm_state is not None:
            try:
                extra["fsm_state"] = await fsm_state.get_state()
            except Exception:
                log.debug("user_activity: failed to read FSM state", exc_info=True)

        log.info("user_event received", extra=extra)
        try:
            result = await handler(event, data)
            duration_ms = int((time.monotonic() - started) * 1000)
            log.info("user_event handled", extra={**extra, "duration_ms": duration_ms})
            return result
        except Exception:
            duration_ms = int((time.monotonic() - started) * 1000)
            log.exception(
                "user_event failed", extra={**extra, "duration_ms": duration_ms}
            )
            raise
