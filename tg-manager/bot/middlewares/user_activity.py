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

# FSM states where message content must NOT be logged (phone/session/code/password input)
_SENSITIVE_STATE_KEYWORDS = (
    "phone",
    "code",
    "password",
    "session",
    "token",
    "2fa",
    "twofa",
    "qr",
    "secret",
    "hash",
    "api_id",
    "api_hash",
)


def _safe_preview(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    redacted = _BOT_TOKEN_RE.sub("<bot_token>", value)
    redacted = _LONG_SECRET_RE.sub("<secret>", redacted)
    return redacted[:limit]


def _is_sensitive_state(state_name: str | None) -> bool:
    if not state_name:
        return False
    lower = state_name.lower()
    return any(kw in lower for kw in _SENSITIVE_STATE_KEYWORDS)


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


def _cb_action(data: str | None) -> str:
    """Extract short action label from callback data."""
    if not data:
        return "callback"
    # Keep first 60 chars — full callback data is useful for debugging
    return data[:60]


def _msg_action(message: Message, fsm_state: str | None) -> tuple[str, str | None]:
    """Returns (action, detail) for a message event."""
    text = message.text or ""
    if text.startswith("/"):
        cmd = text.split()[0][:40]
        return cmd, None
    if fsm_state:
        # In FSM — log the state name, not the content (unless non-sensitive)
        state_short = fsm_state.split(":")[-1] if ":" in fsm_state else fsm_state
        if _is_sensitive_state(fsm_state):
            return f"fsm:{state_short}", None
        detail = _safe_preview(text, 80)
        return f"fsm:{state_short}", detail
    # Plain message outside FSM
    return "message", _safe_preview(text, 60)


class UserActivityLogMiddleware(BaseMiddleware):
    """Log manager-bot messages and callbacks with user, state, and duration.

    Writes to Python logs AND to activity_log table via non-blocking activity_logger.
    """

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

        # Read FSM state before handling
        fsm_state_name: str | None = None
        fsm_context = data.get("state")
        if fsm_context is not None:
            try:
                fsm_state_name = await fsm_context.get_state()
            except Exception:
                log.debug("user_activity: failed to read FSM state", exc_info=True)

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

        if fsm_state_name is not None:
            extra["fsm_state"] = fsm_state_name

        log.info("user_event received", extra=extra)
        status = "ok"
        error_msg: str | None = None
        try:
            result = await handler(event, data)
            duration_ms = int((time.monotonic() - started) * 1000)
            log.info("user_event handled", extra={**extra, "duration_ms": duration_ms})
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            status = "error"
            error_msg = str(exc)[:200]
            log.exception(
                "user_event failed", extra={**extra, "duration_ms": duration_ms}
            )
            raise
        finally:
            # Write to activity_log (fire-and-forget via queue)
            if user_id:
                try:
                    from services import activity_logger

                    if isinstance(event, CallbackQuery):
                        action = _cb_action(event.data)
                        detail = (
                            f"fsm:{fsm_state_name.split(':')[-1]}"
                            if fsm_state_name
                            else None
                        )
                        activity_logger.log_event(
                            user_id,
                            "callback",
                            action,
                            detail,
                            status,
                            error_msg,
                            duration_ms,
                        )
                    elif isinstance(event, Message):
                        action, detail = _msg_action(event, fsm_state_name)
                        event_type = "command" if action.startswith("/") else "message"
                        activity_logger.log_event(
                            user_id,
                            event_type,
                            action,
                            detail,
                            status,
                            error_msg,
                            duration_ms,
                        )
                except Exception:
                    pass  # logging must never break the bot

        return result
