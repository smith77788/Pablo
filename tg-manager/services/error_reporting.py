"""Error reporting with context for debugging and monitoring.

Provides structured error reporting with correlation IDs, user context,
and integration with the activity logger.
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Optional

from services.error_codes import ErrorCode, from_exception, get_user_message
from services.logger import correlation_id, set_correlation_id

log = logging.getLogger(__name__)


@dataclass
class ErrorContext:
    """Context for an error report."""
    error_code: ErrorCode
    message: str
    user_message: str
    user_id: int | None = None
    operation_id: str | None = None
    account_id: int | None = None
    proxy_id: int | None = None
    correlation_id: str = ""
    timestamp: float = field(default_factory=time.time)
    exception_type: str = ""
    exception_traceback: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "user_message": self.user_message,
            "user_id": self.user_id,
            "operation_id": self.operation_id,
            "account_id": self.account_id,
            "proxy_id": self.proxy_id,
            "correlation_id": self.correlation_id,
            "timestamp": self.timestamp,
            "exception_type": self.exception_type,
            "extra": self.extra,
        }


class ErrorReporter:
    """Structured error reporter with context tracking.
    
    Usage:
        reporter = ErrorReporter()
        
        try:
            await do_something()
        except Exception as e:
            context = reporter.report(
                e,
                user_id=12345,
                operation_id="op_abc",
                extra={"account_id": 42}
            )
            # context contains full error details for debugging
    """
    
    def __init__(self):
        self._error_counts: dict[str, int] = {}
        self._recent_errors: list[ErrorContext] = []
        self._max_recent = 1000
    
    def report(
        self,
        exc: Exception,
        user_id: int | None = None,
        operation_id: str | None = None,
        account_id: int | None = None,
        proxy_id: int | None = None,
        extra: dict[str, Any] | None = None,
        log_level: int = logging.ERROR,
    ) -> ErrorContext:
        """Report an error with full context.
        
        Args:
            exc: The exception that occurred
            user_id: Telegram user ID if available
            operation_id: Operation ID if available
            account_id: Account ID if available
            proxy_id: Proxy ID if available
            extra: Additional context
            log_level: Logging level (default: ERROR)
        
        Returns:
            ErrorContext with full error details
        """
        error_code = from_exception(exc)
        user_msg = get_user_message(error_code)
        cid = correlation_id()
        
        context = ErrorContext(
            error_code=error_code,
            message=str(exc),
            user_message=user_msg,
            user_id=user_id,
            operation_id=operation_id,
            account_id=account_id,
            proxy_id=proxy_id,
            correlation_id=cid,
            exception_type=type(exc).__name__,
            exception_traceback=traceback.format_exc(),
            extra=extra or {},
        )
        
        # Track error counts
        code_key = error_code.value
        self._error_counts[code_key] = self._error_counts.get(code_key, 0) + 1
        
        # Store recent errors
        self._recent_errors.append(context)
        if len(self._recent_errors) > self._max_recent:
            self._recent_errors = self._recent_errors[-self._max_recent:]
        
        # Log the error
        self._log_error(context, log_level)
        
        return context
    
    def _log_error(self, context: ErrorContext, level: int) -> None:
        """Log error with structured context."""
        extra = {
            "error_code": context.error_code.value,
            "user_id": context.user_id,
            "operation_id": context.operation_id,
            "account_id": context.account_id,
            "proxy_id": context.proxy_id,
            "correlation_id": context.correlation_id,
        }
        
        if context.extra:
            extra.update(context.extra)
        
        log.log(
            level,
            "[%s] %s (user=%s, op=%s)",
            context.error_code.value,
            context.message,
            context.user_id or "-",
            context.operation_id or "-",
            extra=extra,
        )
    
    def get_error_stats(self) -> dict[str, int]:
        """Get error count statistics."""
        return dict(self._error_counts)
    
    def get_recent_errors(self, limit: int = 10) -> list[ErrorContext]:
        """Get recent errors."""
        return self._recent_errors[-limit:]
    
    def clear_stats(self) -> None:
        """Clear error statistics."""
        self._error_counts.clear()
        self._recent_errors.clear()


# ── Default reporter ──────────────────────────────────────────────────

_default_reporter: ErrorReporter | None = None


def get_error_reporter() -> ErrorReporter:
    """Get the default error reporter."""
    global _default_reporter
    if _default_reporter is None:
        _default_reporter = ErrorReporter()
    return _default_reporter


def report_error(
    exc: Exception,
    user_id: int | None = None,
    operation_id: str | None = None,
    account_id: int | None = None,
    proxy_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> ErrorContext:
    """Convenience function to report an error.
    
    Usage:
        from services.error_reporting import report_error
        
        try:
            await do_something()
        except Exception as e:
            context = report_error(e, user_id=12345)
            # Use context.user_message for user-facing response
    """
    return get_error_reporter().report(
        exc,
        user_id=user_id,
        operation_id=operation_id,
        account_id=account_id,
        proxy_id=proxy_id,
        extra=extra,
    )


def get_user_error_message(exc: Exception, **kwargs: Any) -> str:
    """Get user-facing error message for an exception.
    
    Usage:
        from services.error_reporting import get_user_error_message
        
        try:
            await do_something()
        except Exception as e:
            user_msg = get_user_error_message(e)
            await message.answer(user_msg)
    """
    error_code = from_exception(exc)
    return get_user_message(error_code, **kwargs)
