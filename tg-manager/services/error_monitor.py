"""Error monitoring middleware for tracking and analyzing errors.

Provides error tracking, pattern detection, and alerting.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from services.error_codes import ErrorCode, from_exception, get_user_message
from services.error_reporting import get_error_reporter, ErrorContext

log = logging.getLogger(__name__)


@dataclass
class ErrorPattern:
    """Detected error pattern."""
    error_code: ErrorCode
    count: int
    first_seen: float
    last_seen: float
    affected_users: set[int] = field(default_factory=set)
    sample_messages: list[str] = field(default_factory=list)


class ErrorMonitor:
    """Monitor for tracking error patterns and detecting anomalies.
    
    Usage:
        monitor = ErrorMonitor()
        
        # In middleware or error handler
        monitor.record_error(error_code, user_id=12345, message="...")
        
        # Check for patterns
        patterns = monitor.get_patterns()
        if monitor.should_alert(error_code):
            await send_alert(error_code)
    """
    
    def __init__(self, window_seconds: int = 300, alert_threshold: int = 10):
        self._window_seconds = window_seconds
        self._alert_threshold = alert_threshold
        self._errors: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._patterns: dict[str, ErrorPattern] = {}
        self._last_alert: dict[str, float] = {}
    
    def record_error(
        self,
        error_code: ErrorCode,
        user_id: int | None = None,
        message: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record an error occurrence."""
        now = time.time()
        code_key = error_code.value
        
        self._errors[code_key].append({
            "timestamp": now,
            "user_id": user_id,
            "message": message[:200],
            "extra": extra or {},
        })
        
        # Clean old entries
        cutoff = now - self._window_seconds
        self._errors[code_key] = [
            e for e in self._errors[code_key]
            if e["timestamp"] > cutoff
        ]
        
        # Update pattern
        if code_key not in self._patterns:
            self._patterns[code_key] = ErrorPattern(
                error_code=error_code,
                count=0,
                first_seen=now,
                last_seen=now,
            )
        
        pattern = self._patterns[code_key]
        pattern.count += 1
        pattern.last_seen = now
        if user_id:
            pattern.affected_users.add(user_id)
        if len(pattern.sample_messages) < 5:
            pattern.sample_messages.append(message[:200])
    
    def should_alert(self, error_code: ErrorCode) -> bool:
        """Check if we should send an alert for this error code."""
        code_key = error_code.value
        now = time.time()
        
        # Check cooldown
        last_alert = self._last_alert.get(code_key, 0)
        if now - last_alert < 60:  # 1 minute cooldown
            return False
        
        # Check threshold
        recent_count = len(self._errors.get(code_key, []))
        if recent_count >= self._alert_threshold:
            self._last_alert[code_key] = now
            return True
        
        return False
    
    def get_patterns(self) -> list[ErrorPattern]:
        """Get all detected error patterns."""
        return list(self._patterns.values())
    
    def get_pattern(self, error_code: ErrorCode) -> ErrorPattern | None:
        """Get pattern for a specific error code."""
        return self._patterns.get(error_code.value)
    
    def get_recent_count(self, error_code: ErrorCode) -> int:
        """Get count of recent occurrences for an error code."""
        code_key = error_code.value
        return len(self._errors.get(code_key, []))
    
    def clear_old_patterns(self, max_age_seconds: int = 3600) -> int:
        """Clear patterns older than max_age_seconds. Returns count cleared."""
        now = time.time()
        cutoff = now - max_age_seconds
        
        cleared = 0
        codes_to_remove = []
        
        for code_key, pattern in self._patterns.items():
            if pattern.last_seen < cutoff:
                codes_to_remove.append(code_key)
                cleared += 1
        
        for code_key in codes_to_remove:
            del self._patterns[code_key]
            self._errors.pop(code_key, None)
        
        return cleared


# ── Default monitor ──────────────────────────────────────────────────

_default_monitor: ErrorMonitor | None = None


def get_error_monitor() -> ErrorMonitor:
    """Get the default error monitor."""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = ErrorMonitor()
    return _default_monitor


class ErrorMonitorMiddleware(BaseMiddleware):
    """Middleware for monitoring errors in handlers.
    
    Tracks all errors that occur in handlers and provides
    pattern detection and alerting.
    """
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        monitor = get_error_monitor()
        reporter = get_error_reporter()
        
        # Get user ID
        user_id = None
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id
        
        try:
            return await handler(event, data)
        except Exception as exc:
            # Get error code
            error_code = from_exception(exc)
            
            # Report error
            context = reporter.report(
                exc,
                user_id=user_id,
                extra={"middleware": "error_monitor"},
            )
            
            # Record in monitor
            monitor.record_error(
                error_code,
                user_id=user_id,
                message=str(exc),
            )
            
            # Check if we should alert
            if monitor.should_alert(error_code):
                log.warning(
                    "Error pattern detected: %s (count=%d, users=%d)",
                    error_code.value,
                    monitor.get_recent_count(error_code),
                    len(monitor.get_pattern(error_code).affected_users) if monitor.get_pattern(error_code) else 0,
                )
            
            # Re-raise the exception
            raise


def install_error_monitoring(dp) -> None:
    """Install error monitoring middleware on a dispatcher.
    
    Usage:
        from services.error_monitor import install_error_monitoring
        install_error_monitoring(dp)
    """
    middleware = ErrorMonitorMiddleware()
    dp.message.outer_middleware(middleware)
    dp.callback_query.outer_middleware(middleware)
    log.info("Error monitoring middleware installed")
