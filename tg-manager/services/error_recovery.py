"""Error recovery mechanisms for automatic error handling.

Provides retry logic, circuit breakers, and fallback strategies.

СТАТУС: НЕ ПОДКЛЮЧЁН (проверено 2026-07-27). Дублирует уже работающее:
circuit breaker живёт в `op_worker._circuit_breaker_state`, повторы операций —
в `operation_bus` (`max_retries` в OP_REGISTRY). Прежде чем подключать, решите,
какой из двух механизмов остаётся единственным: два контура повторов над одной
очередью дадут двойной retry.

Запросы трёх действий восстановления были написаны по ВООБРАЖАЕМОЙ схеме
(таблица `operations`, колонки `needs_reauth`, `fail_count`, `updated_at` —
ничего этого в базе нет) и исправлены на настоящую. Это ровно та ловушка, ради
которой существует маркер выше: код выглядел рабочим, а выполниться не мог.
Поведение зафиксировано в tests/test_error_recovery_real_schema_postgres.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Optional

log = logging.getLogger(__name__)


class RetryStrategy(Enum):
    """Retry strategies for error recovery."""
    NONE = "none"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    FIBONACCI = "fibonacci"


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: bool = True
    retryable_exceptions: tuple[type, ...] = (Exception,)


@dataclass
class CircuitBreakerState:
    """State of a circuit breaker."""
    failure_count: int = 0
    last_failure_time: float = 0.0
    is_open: bool = False
    success_count: int = 0


class CircuitBreaker:
    """Circuit breaker pattern for preventing cascade failures.
    
    Usage:
        breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
        
        async def call():
            async with breaker:
                return await external_service()
    """
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._state = CircuitBreakerState()
        self._lock = asyncio.Lock()
    
    @property
    def state(self) -> str:
        """Get current circuit breaker state."""
        if self._state.is_open:
            if time.monotonic() - self._state.last_failure_time > self.recovery_timeout:
                return "half-open"
            return "open"
        return "closed"
    
    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            if self._state.is_open:
                self._state.success_count += 1
                if self._state.success_count >= self.half_open_max_calls:
                    self._state.is_open = False
                    self._state.failure_count = 0
                    self._state.success_count = 0
                    log.info("Circuit breaker closed (recovered)")
            else:
                self._state.failure_count = 0
    
    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._state.failure_count += 1
            self._state.last_failure_time = time.monotonic()
            self._state.success_count = 0
            
            if self._state.failure_count >= self.failure_threshold:
                self._state.is_open = True
                log.warning(
                    "Circuit breaker opened (failures=%d, threshold=%d)",
                    self._state.failure_count,
                    self.failure_threshold,
                )
    
    async def __aenter__(self):
        if self.state == "open":
            raise CircuitBreakerOpenError(
                f"Circuit breaker is open. Retry after {self.recovery_timeout}s"
            )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val is None:
            await self.record_success()
        else:
            await self.record_failure()
        return False


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""
    pass


def _fibonacci(n: int) -> int:
    """Calculate Fibonacci number."""
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


def _calculate_delay(attempt: int, config: RetryConfig) -> float:
    """Calculate delay for retry attempt."""
    import random
    
    if config.strategy == RetryStrategy.LINEAR:
        delay = config.base_delay * attempt
    elif config.strategy == RetryStrategy.EXPONENTIAL:
        delay = config.base_delay * (2 ** attempt)
    elif config.strategy == RetryStrategy.FIBONACCI:
        delay = config.base_delay * _fibonacci(attempt + 1)
    else:
        delay = config.base_delay
    
    delay = min(delay, config.max_delay)
    
    if config.jitter:
        delay *= (0.5 + random.random())
    
    return delay


async def with_retry(
    func: Callable[..., Awaitable[Any]],
    *args: Any,
    config: RetryConfig | None = None,
    **kwargs: Any,
) -> Any:
    """Execute an async function with retry logic.
    
    Usage:
        result = await with_retry(
            fetch_data,
            url="https://api.example.com/data",
            config=RetryConfig(max_retries=3, strategy=RetryStrategy.EXPONENTIAL)
        )
    """
    if config is None:
        config = RetryConfig()
    
    last_exception = None
    
    for attempt in range(config.max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except config.retryable_exceptions as e:
            last_exception = e
            
            if attempt < config.max_retries:
                delay = _calculate_delay(attempt, config)
                log.warning(
                    "Retry attempt %d/%d after %.1fs: %s",
                    attempt + 1,
                    config.max_retries,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)
            else:
                log.error(
                    "All %d retry attempts failed: %s",
                    config.max_retries,
                    e,
                )
    
    raise last_exception


class RecoveryAction:
    """Base class for recovery actions."""
    
    async def can_recover(self, error: Exception) -> bool:
        """Check if this action can recover from the error."""
        return False
    
    async def recover(self, error: Exception, context: dict[str, Any]) -> bool:
        """Attempt to recover from the error. Returns True if successful."""
        return False


class SessionRecoveryAction(RecoveryAction):
    """Recovery action for Telegram session errors."""
    
    async def can_recover(self, error: Exception) -> bool:
        error_str = str(error).lower()
        return any(p in error_str for p in [
            "session",
            "auth",
            "unauthorized",
            "login",
        ])
    
    async def recover(self, error: Exception, context: dict[str, Any]) -> bool:
        account_id = context.get("account_id")
        if not account_id:
            return False
        
        log.info(
            "SessionRecovery: attempting recovery for account %d",
            account_id,
        )
        
        try:
            # Mark account for re-auth
            pool = context.get("pool")
            if pool:
                # Колонок `needs_reauth` и `updated_at` в tg_accounts нет —
                # запрос падал всегда, и аккаунт с мёртвой сессией продолжал
                # разбирать задачи, падая на каждой. Признак «сессия истекла»
                # в этой схеме — acc_status, ровно как ставит монитор сессий.
                await pool.execute(
                    """UPDATE tg_accounts
                       SET acc_status = 'session_expired',
                           status_reason = $2,
                           is_active = FALSE,
                           status_checked_at = NOW()
                       WHERE id = $1""",
                    account_id, str(error)[:300],
                )
                log.info(
                    "SessionRecovery: marked account %d for re-auth",
                    account_id,
                )
                return True
        except Exception as e:
            log.error(
                "SessionRecovery: failed to mark account for re-auth: %s",
                e,
            )
        
        return False


class ProxyRecoveryAction(RecoveryAction):
    """Recovery action for proxy errors."""
    
    async def can_recover(self, error: Exception) -> bool:
        error_str = str(error).lower()
        return any(p in error_str for p in [
            "proxy",
            "socks",
            "connection refused",
        ])
    
    async def recover(self, error: Exception, context: dict[str, Any]) -> bool:
        proxy_id = context.get("proxy_id")
        if not proxy_id:
            return False
        
        log.info(
            "ProxyRecovery: marking proxy %d as failed",
            proxy_id,
        )
        
        try:
            pool = context.get("pool")
            if pool:
                # Колонок `fail_count`, `last_error` и `updated_at` в
                # user_proxies нет — запрос падал всегда, и счётчик сбоев
                # прокси не рос ни разу. Настоящее имя — consecutive_failures,
                # его же читает выбор прокси.
                await pool.execute(
                    """UPDATE user_proxies
                       SET consecutive_failures = COALESCE(consecutive_failures, 0) + 1,
                           is_alive = FALSE,
                           last_checked_at = NOW()
                       WHERE id = $1""",
                    proxy_id,
                )
                return True
        except Exception as e:
            log.error(
                "ProxyRecovery: failed to mark proxy: %s",
                e,
            )
        
        return False


class OperationRecoveryAction(RecoveryAction):
    """Recovery action for operation errors."""
    
    async def can_recover(self, error: Exception) -> bool:
        return isinstance(error, (asyncio.TimeoutError, asyncio.CancelledError))
    
    async def recover(self, error: Exception, context: dict[str, Any]) -> bool:
        op_id = context.get("operation_id")
        if not op_id:
            return False
        
        log.info(
            "OperationRecovery: resetting operation %s",
            op_id,
        )
        
        try:
            pool = context.get("pool")
            if pool:
                # Таблица называется operation_queue; `operations` не существует
                # НИ В ОДНОЙ миграции — запрос падал всегда, и автоматическое
                # восстановление зависшей операции не работало ни разу: она
                # оставалась 'running' до сторожа (60 минут), занимая слот
                # параллельности владельца. Колонок `error`/`updated_at` тут
                # тоже нет — текст ошибки живёт в error_msg/last_error.
                await pool.execute(
                    """UPDATE operation_queue
                       SET status = 'pending', error_msg = NULL, last_error = NULL,
                           started_at = NULL
                       WHERE id = $1 AND status = 'running'""",
                    op_id,
                )
                return True
        except Exception as e:
            log.error(
                "OperationRecovery: failed to reset operation: %s",
                e,
            )
        
        return False


class RecoveryManager:
    """Manager for automatic error recovery.
    
    Usage:
        manager = RecoveryManager()
        manager.register(SessionRecoveryAction())
        manager.register(ProxyRecoveryAction())
        
        try:
            await do_something()
        except Exception as e:
            recovered = await manager.attempt_recovery(e, context)
            if not recovered:
                raise
    """
    
    def __init__(self):
        self._actions: list[RecoveryAction] = []
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
    
    def register(self, action: RecoveryAction) -> None:
        """Register a recovery action."""
        self._actions.append(action)
    
    def get_circuit_breaker(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> CircuitBreaker:
        """Get or create a circuit breaker for a service."""
        if name not in self._circuit_breakers:
            self._circuit_breakers[name] = CircuitBreaker(
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return self._circuit_breakers[name]
    
    async def attempt_recovery(
        self,
        error: Exception,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Attempt to recover from an error.
        
        Returns True if recovery was successful.
        """
        if context is None:
            context = {}
        
        for action in self._actions:
            try:
                if await action.can_recover(error):
                    log.info(
                        "Attempting recovery with %s",
                        type(action).__name__,
                    )
                    if await action.recover(error, context):
                        log.info(
                            "Recovery successful with %s",
                            type(action).__name__,
                        )
                        return True
            except Exception as e:
                log.error(
                    "Recovery action %s failed: %s",
                    type(action).__name__,
                    e,
                )
        
        return False


# ── Default recovery manager ──────────────────────────────────────────

_default_manager: RecoveryManager | None = None


def get_recovery_manager() -> RecoveryManager:
    """Get the default recovery manager."""
    global _default_manager
    if _default_manager is None:
        _default_manager = RecoveryManager()
        _default_manager.register(SessionRecoveryAction())
        _default_manager.register(ProxyRecoveryAction())
        _default_manager.register(OperationRecoveryAction())
    return _default_manager
