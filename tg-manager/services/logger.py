"""Structured logging with correlation_id, timing, and JSON support.

Usage:
    from services.logger import get_logger, correlation_id, timed, set_correlation_id

    log = get_logger(__name__)

    # Set correlation context (call at handler/service entry points)
    set_correlation_id(user_id=12345, op_id="op_abc")
    log.info("Processing operation", extra={"account_id": 42})

    # Measure timing
    with timed(log, "create_channel"):
        result = await create_channel(...)
    # → log.info("create_channel completed in 1.23s", extra={"duration_ms": 1234})

    # As decorator
    @timed(log, "fetch_dialogs")
    async def fetch_dialogs(session_str, _acc=None):
        ...
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import time
import traceback
import uuid
from typing import Any, ClassVar

# ── Correlation context ────────────────────────────────────────────

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "user_id", default=None
)
_op_id: contextvars.ContextVar[str] = contextvars.ContextVar("op_id", default="")


def generate_correlation_id() -> str:
    """Generate a short unique correlation id."""
    return uuid.uuid4().hex[:12]


def set_correlation_id(
    *,
    correlation_id: str | None = None,
    user_id: int | None = None,
    op_id: str | None = None,
) -> str:
    """Set correlation context variables. Returns the active correlation_id."""
    if correlation_id is not None:
        _correlation_id.set(correlation_id)
    elif not _correlation_id.get():
        _correlation_id.set(generate_correlation_id())
    if user_id is not None:
        _user_id.set(user_id)
    if op_id is not None:
        _op_id.set(op_id)
    return _correlation_id.get()


def correlation_id() -> str:
    """Return the current correlation id, generating one if absent."""
    cid = _correlation_id.get()
    if not cid:
        cid = generate_correlation_id()
        _correlation_id.set(cid)
    return cid


# ── JSON formatter ──────────────────────────────────────────────────


class _StructuredFormatter(logging.Formatter):
    """JSON log formatter with extra field support.

    Set LOG_FORMAT=json via env to enable; defaults to human-readable text.
    """

    _reserved: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "levelname",
            "asctime",
            "message",
            "exc_info",
            "exc_text",
        }
    )

    def __init__(self, use_json: bool = False):
        super().__init__()
        self.use_json = use_json

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_json:
            return self._format_text(record)
        return self._format_json(record)

    def _format_text(self, record: logging.LogRecord) -> str:
        """Human-readable: 2026-05-30 12:34:56 [svc.scheduler] WARNING: message  cid=abc123"""
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        cid = _correlation_id.get()
        cid_part = f"  cid={cid}" if cid else ""
        return (
            f"{ts} [{record.name}] {record.levelname}: {record.getMessage()}{cid_part}"
        )

    def _format_json(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "logger": record.name,
            "level": record.levelname,
            "msg": record.getMessage(),
            "correlation_id": _correlation_id.get() or None,
        }
        user_id = _user_id.get()
        if user_id:
            base["user_id"] = user_id
        op_id = _op_id.get()
        if op_id:
            base["op_id"] = op_id

        if record.exc_info and record.exc_info[1]:
            base["exc"] = str(record.exc_info[1])
            base["exc_type"] = type(record.exc_info[1]).__name__

        # Merge extra fields from record.__dict__
        extra: dict[str, Any] = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._reserved and not k.startswith("_")
        }
        if extra:
            base["extra"] = extra

        return json.dumps(base, default=str, ensure_ascii=False)


def configure_root_logger(level: int = logging.INFO, use_json: bool = False) -> None:
    """Configure the root logger with structured formatting.

    Call once in main.py. Pass use_json=True for JSON output in production.
    """
    root = logging.getLogger()
    root.setLevel(level)
    # Remove any existing handlers
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(_StructuredFormatter(use_json=use_json))
    root.addHandler(handler)


# ── Logger factory ─────────────────────────────────────────────────


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured for structured logging.

    Usage: log = get_logger(__name__)
    """
    return logging.getLogger(name)


# ── Timing utilities ───────────────────────────────────────────────


class _Timed:
    """Context manager / decorator for timing operations.

    Context manager:
        with timed(log, "create_channel", extra={"acc_id": 1}):
            await create_channel(...)

    Decorator:
        @timed(log, "fetch_dialogs")
        async def fetch_dialogs(...): ...
    """

    __slots__ = ("_log", "_label", "_extra", "_level", "_start", "_duration_ms")

    def __init__(
        self,
        log: logging.Logger,
        label: str,
        *,
        extra: dict[str, Any] | None = None,
        level: int = logging.DEBUG,
    ):
        self._log = log
        self._label = label
        self._extra = extra or {}
        self._level = level
        self._start: float = 0.0
        self._duration_ms: int = 0

    def __enter__(self) -> _Timed:
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._duration_ms = int((time.monotonic() - self._start) * 1000)
        if exc_val is None:
            self._log.log(
                self._level,
                "%s completed in %dms",
                self._label,
                self._duration_ms,
                extra={**self._extra, "duration_ms": self._duration_ms},
            )
        else:
            self._log.warning(
                "%s failed after %dms: %s",
                self._label,
                self._duration_ms,
                exc_val,
                extra={**self._extra, "duration_ms": self._duration_ms},
            )

    def __call__(self, fn):
        """Decorator mode."""
        is_async = asyncio.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            with self:
                return await fn(*args, **kwargs)

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)

        return async_wrapper if is_async else sync_wrapper


# Need asyncio for decorator detection
import asyncio  # noqa: E402


def timed(
    log: logging.Logger,
    label: str,
    *,
    extra: dict[str, Any] | None = None,
    level: int = logging.DEBUG,
) -> _Timed:
    """Create a timing context manager / decorator.

    Usage:
        with timed(log, "upsert_users", extra={"count": len(users)}) as t:
            await do_upsert()
        log.info("Took %dms", t.duration_ms)
    """
    return _Timed(log, label, extra=extra, level=level)


# ── Safe-log helpers ────────────────────────────────────────────────


def log_exc_swallow(
    log: logging.Logger,
    msg: str,
    *args,
    level: int = logging.DEBUG,
    **extra: Any,
) -> None:
    """Log swallowed exceptions — drop-in for `except Exception: pass`.

    Replace:
        except Exception:
            pass
    With:
        except Exception:
            log_exc_swallow(log, "Optional context message", account_id=acc_id)
    """
    try:
        log.log(
            level,
            "%s  | swallowed: %s",
            msg,
            traceback.format_exc().strip().split("\n")[-1],
            extra=extra,
        )
    except Exception:
        log.debug("log_exc_swallow itself failed: %s", msg, exc_info=True)


def log_exc_silent(
    log: logging.Logger,
    msg: str,
    *args,
    **extra: Any,
) -> None:
    """Like log_exc_swallow but with exc_info for full traceback in debug."""
    log.debug("%s  | traceback follows", msg, exc_info=True, extra=extra)
