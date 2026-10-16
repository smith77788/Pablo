"""Per-request signal for handlers to mark a user-visible error.

When a handler catches an exception and shows "❌ Ошибка..." to the user
(instead of re-raising), the activity middleware would normally log the
event as status='ok' — making the error invisible in logs.

Handlers call mark_handled_error(msg) to signal the middleware that the
request completed with a user-visible error, so it gets logged as
status='warning' instead of status='ok'.

Uses ContextVar so it's safe for concurrent async requests — each asyncio
Task gets its own copy of the variable.
"""

from __future__ import annotations

from contextvars import ContextVar

_handled_error: ContextVar[str | None] = ContextVar("_handled_error", default=None)


def mark_handled_error(msg: str) -> None:
    """Call when catching an exception and showing an error message to the user."""
    _handled_error.set(str(msg)[:200] if msg else None)


def get_handled_error() -> str | None:
    return _handled_error.get()


def clear_handled_error() -> None:
    _handled_error.set(None)
