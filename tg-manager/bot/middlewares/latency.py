"""Latency logging middleware — logs slow handlers (>500ms) to WARNING."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger("latency")

# Threshold in seconds above which we log a warning
_WARN_THRESHOLD = 0.5
_DEBUG_THRESHOLD = 0.1


class LatencyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        t0 = time.monotonic()
        result = await handler(event, data)
        elapsed = time.monotonic() - t0

        if elapsed < _DEBUG_THRESHOLD:
            return result

        if isinstance(event, CallbackQuery):
            label = f"cb:{event.data or '?'}"
            user_id = event.from_user.id if event.from_user else 0
        elif isinstance(event, Message):
            cmd = (event.text or "")[:30] if event.text else "<media>"
            label = f"msg:{cmd}"
            user_id = event.from_user.id if event.from_user else 0
        else:
            label = type(event).__name__
            user_id = 0

        ms = int(elapsed * 1000)
        if elapsed >= _WARN_THRESHOLD:
            log.warning("SLOW %s user=%s duration=%dms", label, user_id, ms)
        else:
            log.debug("ok   %s user=%s duration=%dms", label, user_id, ms)

        return result
