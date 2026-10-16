# Error Handling System

This document describes the comprehensive error handling system for tg-manager.

## Overview

The error handling system provides:
1. **Error Codes** - Unique identifiers for debugging
2. **Error Recovery** - Automatic retry and recovery mechanisms
3. **Error Reporting** - Structured error reporting with context
4. **Error Monitoring** - Pattern detection and alerting
5. **User-Friendly Messages** - Russian-language error messages for users

## Modules

### 1. Error Codes (`services/error_codes.py`)

Error codes are organized by category:
- `AUTH_001-010` - Authentication & Authorization
- `DB_001-008` - Database
- `TG_001-010` - Telegram API
- `NET_001-007` - Network & External Services
- `CLT_001-010` - Telethon Client
- `OP_001-008` - Operations & Queue
- `PAY_001-008` - Payment & Billing
- `VAL_001-005` - Validation
- `INT_001-099` - Internal & System

**Usage:**

```python
from services.error_codes import ErrorCode, get_user_message

# Get error code from exception
error_code = ErrorCode.from_exception(exc)

# Get user-facing message (Russian)
user_msg = get_user_message(error_code)

# Raise application error
from services.error_codes import AppError
raise AppError(ErrorCode.AUTH_FAILED, user_id=12345)
```

### 2. Error Recovery (`services/error_recovery.py`)

Provides retry logic, circuit breakers, and fallback strategies.

**Retry with exponential backoff:**

```python
from services.error_recovery import with_retry, RetryConfig, RetryStrategy

config = RetryConfig(
    max_retries=3,
    strategy=RetryStrategy.EXPONENTIAL,
    base_delay=1.0,
)

result = await with_retry(fetch_data, url="...", config=config)
```

**Circuit breaker:**

```python
from services.error_recovery import CircuitBreaker

breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

async with breaker:
    result = await external_service()
```

**Recovery manager:**

```python
from services.error_recovery import get_recovery_manager

manager = get_recovery_manager()

try:
    await do_something()
except Exception as e:
    recovered = await manager.attempt_recovery(e, context)
    if not recovered:
        raise
```

### 3. Error Reporting (`services/error_reporting.py`)

Structured error reporting with context tracking.

**Report an error:**

```python
from services.error_reporting import report_error

try:
    await do_something()
except Exception as e:
    context = report_error(
        e,
        user_id=12345,
        operation_id="op_abc",
        extra={"account_id": 42},
    )
    # context.user_message contains user-facing message
```

**Get user-friendly message:**

```python
from services.error_reporting import get_user_error_message

try:
    await do_something()
except Exception as e:
    user_msg = get_user_error_message(e)
    await message.answer(user_msg)
```

### 4. Error Monitoring (`services/error_monitor.py`)

Pattern detection and alerting for errors.

**Install middleware:**

```python
from services.error_monitor import install_error_monitoring

install_error_monitoring(dp)
```

**Manual monitoring:**

```python
from services.error_monitor import get_error_monitor

monitor = get_error_monitor()

# Record error
monitor.record_error(ErrorCode.TG_FLOOD_LIMIT, user_id=12345)

# Check for patterns
if monitor.should_alert(ErrorCode.TG_FLOOD_LIMIT):
    await send_alert()
```

## Integration with Existing Code

### Global Error Handler (main.py)

The global error handler now uses error codes:

```python
async def _global_error_handler(event: ErrorEvent) -> None:
    exc = event.exception
    error_code = ErrorCode.from_exception(exc)
    user_msg = get_user_message(error_code)
    
    # Report error with context
    report_error(exc, user_id=user_id)
    
    # Show user-friendly message with error code
    await message.answer(
        f"⚠️ <b>Ошибка [{error_code.value}]</b>\n\n"
        f"{user_msg}",
        parse_mode="HTML",
    )
```

### Database Helpers (op_worker.py)

Safe DB helpers now report errors:

```python
async def _safe_execute(pool, query, *args, log_ctx="") -> str:
    try:
        return await pool.execute(query, *args)
    except Exception as e:
        report_error(e, extra={"context": f"op_worker{log_ctx}"})
        return "ERROR"
```

### Account Manager (account_manager.py)

Proxy parsing now uses error reporting:

```python
except Exception as e:
    report_error(e, extra={"proxy_url": "...", "context": "proxy_parse"})
    return None
```

## Error Code Format

Error codes follow the format: `CATEGORY_NUMBER`

Examples:
- `AUTH_001` - Authentication failed
- `DB_002` - Database query timeout
- `TG_006` - Telegram flood limit
- `NET_005` - Proxy connection failed
- `CLT_009` - Session corrupted

## User-Facing Messages

All user-facing messages are in Russian and follow the format:
- Start with a brief description
- Include actionable advice when possible
- Include error code in brackets for support reference

Example:
```
⚠️ Ошибка [AUTH_008]

Сессия Telegram истекла. Переподключите аккаунт.
```

## Best Practices

1. **Always use error codes** - Map exceptions to specific codes
2. **Report errors** - Use `report_error()` for structured reporting
3. **Use user messages** - Always show user-friendly messages, not raw exceptions
4. **Include context** - Add user_id, operation_id, etc. for debugging
5. **Use recovery mechanisms** - Implement retry logic for transient errors
6. **Monitor patterns** - Use error monitor to detect anomalies

## Migration Guide

### Before (old pattern):
```python
try:
    await do_something()
except Exception as e:
    log.error("Failed: %s", e)
    await message.answer("❌ Ошибка. Попробуйте позже.")
```

### After (new pattern):
```python
from services.error_codes import ErrorCode
from services.error_reporting import report_error, get_user_error_message

try:
    await do_something()
except Exception as e:
    report_error(e, user_id=user_id)
    user_msg = get_user_error_message(e)
    await message.answer(f"❌ {user_msg}")
```
