"""Error codes for debugging and user-facing error messages.

Usage:
    from services.error_codes import ErrorCode, get_error_message, get_user_message

    # Get error code for an exception
    error_code = ErrorCode.from_exception(exc)

    # Get user-friendly message (in Russian)
    user_msg = get_user_message(error_code)

    # Get technical message for logging
    tech_msg = get_error_message(error_code)
"""

from __future__ import annotations
import logging
from enum import Enum
from json import JSONDecodeError
from typing import Any, Optional

log = logging.getLogger(__name__)


class ErrorCode(Enum):
    """Error codes organized by category."""
    
    # ── Authentication & Authorization (1xxx) ──────────────────────
    AUTH_FAILED = "AUTH_001"
    AUTH_TOKEN_EXPIRED = "AUTH_002"
    AUTH_TOKEN_INVALID = "AUTH_003"
    AUTH_PERMISSION_DENIED = "AUTH_004"
    AUTH_2FA_REQUIRED = "AUTH_005"
    AUTH_PHONE_INVALID = "AUTH_006"
    AUTH_CODE_INVALID = "AUTH_007"
    AUTH_SESSION_EXPIRED = "AUTH_008"
    AUTH_BANNED = "AUTH_009"
    AUTH_FLOOD_WAIT = "AUTH_010"
    
    # ── Database (2xxx) ────────────────────────────────────────────
    DB_CONNECTION_FAILED = "DB_001"
    DB_QUERY_TIMEOUT = "DB_002"
    DB_CONSTRAINT_VIOLATION = "DB_003"
    DB_INTEGRITY_ERROR = "DB_004"
    DB_POOL_EXHAUSTED = "DB_005"
    DB_MIGRATION_FAILED = "DB_006"
    DB_RECORD_NOT_FOUND = "DB_007"
    DB_DUPLICATE_ENTRY = "DB_008"
    
    # ── Telegram API (3xxx) ────────────────────────────────────────
    TG_API_FAILED = "TG_001"
    TG_MESSAGE_TOO_LONG = "TG_002"
    TG_MESSAGE_NOT_MODIFIED = "TG_003"
    TG_CHAT_NOT_FOUND = "TG_004"
    TG_USER_BLOCKED = "TG_005"
    TG_FLOOD_LIMIT = "TG_006"
    TG_INVALID_PEER = "TG_007"
    TG_PHOTO_NOT_FOUND = "TG_008"
    TG_FILE_TOO_BIG = "TG_009"
    TG_WEBHOOK_FAILED = "TG_010"
    
    # ── Network & External Services (4xxx) ─────────────────────────
    NET_CONNECTION_FAILED = "NET_001"
    NET_TIMEOUT = "NET_002"
    NET_DNS_RESOLUTION = "NET_003"
    NET_SSL_ERROR = "NET_004"
    NET_PROXY_FAILED = "NET_005"
    NET_RATE_LIMITED = "NET_006"
    NET_SERVICE_UNAVAILABLE = "NET_007"
    
    # ── Telethon Client (5xxx) ─────────────────────────────────────
    CLIENT_CONNECT_FAILED = "CLT_001"
    CLIENT_DISCONNECTED = "CLT_002"
    CLIENT_AUTH_REQUIRED = "CLT_003"
    CLIENT_PHONE_INVALID = "CLT_004"
    CLIENT_CODE_INVALID = "CLT_005"
    CLIENT_2FA_REQUIRED = "CLT_006"
    CLIENT_FLOOD_WAIT = "CLT_007"
    CLIENT_BANNED = "CLT_008"
    CLIENT_SESSION_CORRUPT = "CLT_009"
    CLIENT_PROXY_FAILED = "CLT_010"
    
    # ── Operations & Queue (6xxx) ──────────────────────────────────
    OP_NOT_FOUND = "OP_001"
    OP_ALREADY_RUNNING = "OP_002"
    OP_TIMEOUT = "OP_003"
    OP_CANCELLED = "OP_004"
    OP_DEPENDENCY_FAILED = "OP_005"
    OP_RESOURCE_UNAVAILABLE = "OP_006"
    OP_QUEUE_FULL = "OP_007"
    OP_RATE_LIMITED = "OP_008"
    
    # ── Payment & Billing (7xxx) ───────────────────────────────────
    PAYMENT_FAILED = "PAY_001"
    PAYMENT_INSUFFICIENT = "PAY_002"
    PAYMENT_TIMEOUT = "PAY_003"
    PAYMENT_INVALID_ADDRESS = "PAY_004"
    PAYMENT_NETWORK_MISMATCH = "PAY_005"
    PAYMENT_NOT_FOUND = "PAY_006"
    SUBSCRIPTION_EXPIRED = "PAY_007"
    SUBSCRIPTION_LIMIT_REACHED = "PAY_008"
    
    # ── Validation (8xxx) ─────────────────────────────────────────
    VAL_INVALID_INPUT = "VAL_001"
    VAL_MISSING_REQUIRED = "VAL_002"
    VAL_OUT_OF_RANGE = "VAL_003"
    VAL_INVALID_FORMAT = "VAL_004"
    VAL_CONSTRAINT_VIOLATION = "VAL_005"
    
    # ── Internal & System (9xxx) ───────────────────────────────────
    INT_INTERNAL_ERROR = "INT_001"
    INT_NOT_IMPLEMENTED = "INT_002"
    INT_CONFIGURATION_ERROR = "INT_003"
    INT_RESOURCE_EXHAUSTED = "INT_004"
    INT_SERVICE_UNAVAILABLE = "INT_005"
    INT_DEPENDENCY_MISSING = "INT_006"
    INT_UNEXPECTED_ERROR = "INT_099"


# ── User-facing messages (Russian) ──────────────────────────────────

_USER_MESSAGES: dict[ErrorCode, str] = {
    # Auth
    ErrorCode.AUTH_FAILED: "Не удалось авторизоваться. Попробуйте снова.",
    ErrorCode.AUTH_TOKEN_EXPIRED: "Сессия истекла. Войдите заново.",
    ErrorCode.AUTH_TOKEN_INVALID: "Неверный токен авторизации.",
    ErrorCode.AUTH_PERMISSION_DENIED: "У вас нет прав для этого действия.",
    ErrorCode.AUTH_2FA_REQUIRED: "Требуется двухфакторная аутентификация.",
    ErrorCode.AUTH_PHONE_INVALID: "Неверный формат номера телефона.",
    ErrorCode.AUTH_CODE_INVALID: "Неверный код подтверждения.",
    ErrorCode.AUTH_SESSION_EXPIRED: "Сессия Telegram истекла. Переподключите аккаунт.",
    ErrorCode.AUTH_BANNED: "Аккаунт заблокирован Telegram.",
    ErrorCode.AUTH_FLOOD_WAIT: "Слишком много запросов. Подождите {seconds} сек.",
    
    # Database
    ErrorCode.DB_CONNECTION_FAILED: "Ошибка подключения к базе данных. Попробуйте позже.",
    ErrorCode.DB_QUERY_TIMEOUT: "Запрос выполняется слишком долго. Попробуйте упростить операцию.",
    ErrorCode.DB_CONSTRAINT_VIOLATION: "Нарушение ограничений базы данных.",
    ErrorCode.DB_INTEGRITY_ERROR: "Ошибка целостности данных.",
    ErrorCode.DB_POOL_EXHAUSTED: "Сервис временно перегружен. Попробуйте позже.",
    ErrorCode.DB_MIGRATION_FAILED: "Ошибка обновления системы. Обратитесь в поддержку.",
    ErrorCode.DB_RECORD_NOT_FOUND: "Запись не найдена.",
    ErrorCode.DB_DUPLICATE_ENTRY: "Такая запись уже существует.",
    
    # Telegram
    ErrorCode.TG_API_FAILED: "Ошибка Telegram API. Попробуйте позже.",
    ErrorCode.TG_MESSAGE_TOO_LONG: "Сообщение слишком длинное. Сократите текст.",
    ErrorCode.TG_MESSAGE_NOT_MODIFIED: "Сообщение не изменилось.",
    ErrorCode.TG_CHAT_NOT_FOUND: "Чат не найден.",
    ErrorCode.TG_USER_BLOCKED: "Пользователь заблокировал бота.",
    ErrorCode.TG_FLOOD_LIMIT: "Превышен лимит Telegram. Подождите {seconds} сек.",
    ErrorCode.TG_INVALID_PEER: "Неверный получатель.",
    ErrorCode.TG_PHOTO_NOT_FOUND: "Фото не найдено.",
    ErrorCode.TG_FILE_TOO_BIG: "Файл слишком большой. Максимум 50 МБ.",
    ErrorCode.TG_WEBHOOK_FAILED: "Ошибка webhook. Проверьте настройки.",
    
    # Network
    ErrorCode.NET_CONNECTION_FAILED: "Не удалось подключиться к серверу.",
    ErrorCode.NET_TIMEOUT: "Превышено время ожидания. Проверьте соединение.",
    ErrorCode.NET_DNS_RESOLUTION: "Не удалось разрешить имя сервера.",
    ErrorCode.NET_SSL_ERROR: "Ошибка SSL-сертификата.",
    ErrorCode.NET_PROXY_FAILED: "Прокси-сервер недоступен.",
    ErrorCode.NET_RATE_LIMITED: "Превышен лимит запросов. Подождите.",
    ErrorCode.NET_SERVICE_UNAVAILABLE: "Сервис временно недоступен. Попробуйте позже.",
    
    # Client
    ErrorCode.CLIENT_CONNECT_FAILED: "Не удалось подключиться к Telegram.",
    ErrorCode.CLIENT_DISCONNECTED: "Соединение с Telegram потеряно.",
    ErrorCode.CLIENT_AUTH_REQUIRED: "Требуется авторизация в Telegram.",
    ErrorCode.CLIENT_PHONE_INVALID: "Неверный номер телефона.",
    ErrorCode.CLIENT_CODE_INVALID: "Неверный код подтверждения.",
    ErrorCode.CLIENT_2FA_REQUIRED: "Требуется пароль двухфакторной аутентификации.",
    ErrorCode.CLIENT_FLOOD_WAIT: "Telegram запросил паузу. Подождите {seconds} сек.",
    ErrorCode.CLIENT_BANNED: "Аккаунт заблокирован Telegram.",
    ErrorCode.CLIENT_SESSION_CORRUPT: "Повреждена сессия. Переподключите аккаунт.",
    ErrorCode.CLIENT_PROXY_FAILED: "Прокси-сервер недоступен. Проверьте настройки.",
    
    # Operations
    ErrorCode.OP_NOT_FOUND: "Операция не найдена.",
    ErrorCode.OP_ALREADY_RUNNING: "Операция уже выполняется.",
    ErrorCode.OP_TIMEOUT: "Операция превысила время ожидания.",
    ErrorCode.OP_CANCELLED: "Операция отменена.",
    ErrorCode.OP_DEPENDENCY_FAILED: "Операция не может выполниться из-за сбоя зависимости.",
    ErrorCode.OP_RESOURCE_UNAVAILABLE: "Нет доступных ресурсов для операции.",
    ErrorCode.OP_QUEUE_FULL: "Очередь операций заполнена. Попробуйте позже.",
    ErrorCode.OP_RATE_LIMITED: "Слишком много операций. Подождите.",
    
    # Payment
    ErrorCode.PAYMENT_FAILED: "Ошибка оплаты. Проверьте данные и попробуйте снова.",
    ErrorCode.PAYMENT_INSUFFICIENT: "Недостаточно средств.",
    ErrorCode.PAYMENT_TIMEOUT: "Время оплаты истекло. Начните заново.",
    ErrorCode.PAYMENT_INVALID_ADDRESS: "Неверный адрес кошелька.",
    ErrorCode.PAYMENT_NETWORK_MISMATCH: "Неверная сеть для оплаты.",
    ErrorCode.PAYMENT_NOT_FOUND: "Платёж не найден.",
    ErrorCode.SUBSCRIPTION_EXPIRED: "Подписка истекла. Продлите для продолжения.",
    ErrorCode.SUBSCRIPTION_LIMIT_REACHED: "Достигнут лимит подписки.",
    
    # Validation
    ErrorCode.VAL_INVALID_INPUT: "Неверные входные данные.",
    ErrorCode.VAL_MISSING_REQUIRED: "Не заполнены обязательные поля.",
    ErrorCode.VAL_OUT_OF_RANGE: "Значение вне допустимого диапазона.",
    ErrorCode.VAL_INVALID_FORMAT: "Неверный формат данных.",
    ErrorCode.VAL_CONSTRAINT_VIOLATION: "Нарушение ограничений валидации.",
    
    # Internal
    ErrorCode.INT_INTERNAL_ERROR: "Внутренняя ошибка. Попробуйте позже.",
    ErrorCode.INT_NOT_IMPLEMENTED: "Функция ещё не реализована.",
    ErrorCode.INT_CONFIGURATION_ERROR: "Ошибка конфигурации. Обратитесь в поддержку.",
    ErrorCode.INT_RESOURCE_EXHAUSTED: "Сервис перегружен. Попробуйте позже.",
    ErrorCode.INT_SERVICE_UNAVAILABLE: "Сервис временно недоступен.",
    ErrorCode.INT_DEPENDENCY_MISSING: "Отсутствует необходимая зависимость.",
    ErrorCode.INT_UNEXPECTED_ERROR: "Непредвиденная ошибка. Обратитесь в поддержку.",
}


# ── Technical messages (English) ─────────────────────────────────────

_TECHNICAL_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_FAILED: "Authentication failed",
    ErrorCode.AUTH_TOKEN_EXPIRED: "Auth token expired",
    ErrorCode.AUTH_TOKEN_INVALID: "Invalid auth token",
    ErrorCode.AUTH_PERMISSION_DENIED: "Permission denied",
    ErrorCode.AUTH_2FA_REQUIRED: "Two-factor authentication required",
    ErrorCode.AUTH_PHONE_INVALID: "Invalid phone number format",
    ErrorCode.AUTH_CODE_INVALID: "Invalid confirmation code",
    ErrorCode.AUTH_SESSION_EXPIRED: "Telegram session expired",
    ErrorCode.AUTH_BANNED: "Account banned by Telegram",
    ErrorCode.AUTH_FLOOD_WAIT: "Flood wait: {seconds}s",
    
    ErrorCode.DB_CONNECTION_FAILED: "Database connection failed",
    ErrorCode.DB_QUERY_TIMEOUT: "Database query timeout",
    ErrorCode.DB_CONSTRAINT_VIOLATION: "Database constraint violation",
    ErrorCode.DB_INTEGRITY_ERROR: "Database integrity error",
    ErrorCode.DB_POOL_EXHAUSTED: "Database connection pool exhausted",
    ErrorCode.DB_MIGRATION_FAILED: "Database migration failed",
    ErrorCode.DB_RECORD_NOT_FOUND: "Record not found",
    ErrorCode.DB_DUPLICATE_ENTRY: "Duplicate entry",
    
    ErrorCode.TG_API_FAILED: "Telegram API error",
    ErrorCode.TG_MESSAGE_TOO_LONG: "Message too long",
    ErrorCode.TG_MESSAGE_NOT_MODIFIED: "Message not modified",
    ErrorCode.TG_CHAT_NOT_FOUND: "Chat not found",
    ErrorCode.TG_USER_BLOCKED: "User blocked the bot",
    ErrorCode.TG_FLOOD_LIMIT: "Telegram flood limit: {seconds}s",
    ErrorCode.TG_INVALID_PEER: "Invalid peer",
    ErrorCode.TG_PHOTO_NOT_FOUND: "Photo not found",
    ErrorCode.TG_FILE_TOO_BIG: "File too big (max 50MB)",
    ErrorCode.TG_WEBHOOK_FAILED: "Webhook failed",
    
    ErrorCode.NET_CONNECTION_FAILED: "Connection failed",
    ErrorCode.NET_TIMEOUT: "Network timeout",
    ErrorCode.NET_DNS_RESOLUTION: "DNS resolution failed",
    ErrorCode.NET_SSL_ERROR: "SSL error",
    ErrorCode.NET_PROXY_FAILED: "Proxy connection failed",
    ErrorCode.NET_RATE_LIMITED: "Rate limited",
    ErrorCode.NET_SERVICE_UNAVAILABLE: "Service unavailable",
    
    ErrorCode.CLIENT_CONNECT_FAILED: "Client connection failed",
    ErrorCode.CLIENT_DISCONNECTED: "Client disconnected",
    ErrorCode.CLIENT_AUTH_REQUIRED: "Client authentication required",
    ErrorCode.CLIENT_PHONE_INVALID: "Invalid phone number",
    ErrorCode.CLIENT_CODE_INVALID: "Invalid code",
    ErrorCode.CLIENT_2FA_REQUIRED: "2FA password required",
    ErrorCode.CLIENT_FLOOD_WAIT: "Client flood wait: {seconds}s",
    ErrorCode.CLIENT_BANNED: "Client banned",
    ErrorCode.CLIENT_SESSION_CORRUPT: "Session corrupted",
    ErrorCode.CLIENT_PROXY_FAILED: "Client proxy failed",
    
    ErrorCode.OP_NOT_FOUND: "Operation not found",
    ErrorCode.OP_ALREADY_RUNNING: "Operation already running",
    ErrorCode.OP_TIMEOUT: "Operation timeout",
    ErrorCode.OP_CANCELLED: "Operation cancelled",
    ErrorCode.OP_DEPENDENCY_FAILED: "Operation dependency failed",
    ErrorCode.OP_RESOURCE_UNAVAILABLE: "No resources available",
    ErrorCode.OP_QUEUE_FULL: "Operation queue full",
    ErrorCode.OP_RATE_LIMITED: "Operation rate limited",
    
    ErrorCode.PAYMENT_FAILED: "Payment failed",
    ErrorCode.PAYMENT_INSUFFICIENT: "Insufficient funds",
    ErrorCode.PAYMENT_TIMEOUT: "Payment timeout",
    ErrorCode.PAYMENT_INVALID_ADDRESS: "Invalid wallet address",
    ErrorCode.PAYMENT_NETWORK_MISMATCH: "Payment network mismatch",
    ErrorCode.PAYMENT_NOT_FOUND: "Payment not found",
    ErrorCode.SUBSCRIPTION_EXPIRED: "Subscription expired",
    ErrorCode.SUBSCRIPTION_LIMIT_REACHED: "Subscription limit reached",
    
    ErrorCode.VAL_INVALID_INPUT: "Invalid input",
    ErrorCode.VAL_MISSING_REQUIRED: "Missing required fields",
    ErrorCode.VAL_OUT_OF_RANGE: "Value out of range",
    ErrorCode.VAL_INVALID_FORMAT: "Invalid format",
    ErrorCode.VAL_CONSTRAINT_VIOLATION: "Validation constraint violated",
    
    ErrorCode.INT_INTERNAL_ERROR: "Internal error",
    ErrorCode.INT_NOT_IMPLEMENTED: "Not implemented",
    ErrorCode.INT_CONFIGURATION_ERROR: "Configuration error",
    ErrorCode.INT_RESOURCE_EXHAUSTED: "Resource exhausted",
    ErrorCode.INT_SERVICE_UNAVAILABLE: "Service unavailable",
    ErrorCode.INT_DEPENDENCY_MISSING: "Missing dependency",
    ErrorCode.INT_UNEXPECTED_ERROR: "Unexpected error",
}


# ── Exception to ErrorCode mapping ────────────────────────────────────

_EXCEPTION_MAP: dict[type, ErrorCode] = {
    # Auth
    PermissionError: ErrorCode.AUTH_PERMISSION_DENIED,
    TimeoutError: ErrorCode.NET_TIMEOUT,
    ConnectionError: ErrorCode.NET_CONNECTION_FAILED,
    ConnectionRefusedError: ErrorCode.NET_CONNECTION_FAILED,
    ConnectionResetError: ErrorCode.NET_CONNECTION_FAILED,
    ConnectionAbortedError: ErrorCode.NET_CONNECTION_FAILED,
    OSError: ErrorCode.NET_CONNECTION_FAILED,
    FileNotFoundError: ErrorCode.INT_DEPENDENCY_MISSING,
    ImportError: ErrorCode.INT_DEPENDENCY_MISSING,
    ModuleNotFoundError: ErrorCode.INT_DEPENDENCY_MISSING,
    ValueError: ErrorCode.VAL_INVALID_INPUT,
    TypeError: ErrorCode.VAL_INVALID_INPUT,
    KeyError: ErrorCode.VAL_MISSING_REQUIRED,
    IndexError: ErrorCode.VAL_OUT_OF_RANGE,
    AttributeError: ErrorCode.INT_INTERNAL_ERROR,
    RuntimeError: ErrorCode.INT_UNEXPECTED_ERROR,
    NotImplementedError: ErrorCode.INT_NOT_IMPLEMENTED,
    MemoryError: ErrorCode.INT_RESOURCE_EXHAUSTED,
    RecursionError: ErrorCode.INT_RESOURCE_EXHAUSTED,
    OverflowError: ErrorCode.VAL_OUT_OF_RANGE,
    ZeroDivisionError: ErrorCode.INT_INTERNAL_ERROR,
    UnicodeDecodeError: ErrorCode.VAL_INVALID_FORMAT,
    JSONDecodeError: ErrorCode.VAL_INVALID_FORMAT,
}


def from_exception(exc: Exception) -> ErrorCode:
    """Map an exception to an ErrorCode."""
    exc_type = type(exc)
    
    # Check exact type match first
    if exc_type in _EXCEPTION_MAP:
        return _EXCEPTION_MAP[exc_type]
    
    # Check parent classes
    for parent_type, code in _EXCEPTION_MAP.items():
        if isinstance(exc, parent_type):
            return code
    
    # Check exception string patterns for more specific codes
    exc_str = str(exc).lower()
    
    # Telegram-specific patterns
    if "flood" in exc_str and "wait" in exc_str:
        return ErrorCode.TG_FLOOD_LIMIT
    if "message is not modified" in exc_str:
        return ErrorCode.TG_MESSAGE_NOT_MODIFIED
    if "message too long" in exc_str:
        return ErrorCode.TG_MESSAGE_TOO_LONG
    if "chat not found" in exc_str:
        return ErrorCode.TG_CHAT_NOT_FOUND
    if "blocked" in exc_str or "user is blocked" in exc_str:
        return ErrorCode.TG_USER_BLOCKED
    if "query is too old" in exc_str or "query_id_invalid" in exc_str:
        return ErrorCode.TG_API_FAILED
    
    # Database patterns
    if "connection" in exc_str and ("refused" in exc_str or "failed" in exc_str):
        return ErrorCode.DB_CONNECTION_FAILED
    if "timeout" in exc_str and ("query" in exc_str or "db" in exc_str):
        return ErrorCode.DB_QUERY_TIMEOUT
    if "unique" in exc_str and ("violation" in exc_str or "constraint" in exc_str):
        return ErrorCode.DB_DUPLICATE_ENTRY
    if "foreign key" in exc_str:
        return ErrorCode.DB_CONSTRAINT_VIOLATION
    if "pool" in exc_str and "exhausted" in exc_str:
        return ErrorCode.DB_POOL_EXHAUSTED
    
    # Network patterns
    if "ssl" in exc_str:
        return ErrorCode.NET_SSL_ERROR
    if "proxy" in exc_str and ("failed" in exc_str or "error" in exc_str):
        return ErrorCode.NET_PROXY_FAILED
    if "rate" in exc_str and "limit" in exc_str:
        return ErrorCode.NET_RATE_LIMITED
    if "dns" in exc_str:
        return ErrorCode.NET_DNS_RESOLUTION
    
    # Client patterns
    if "session" in exc_str and ("corrupt" in exc_str or "invalid" in exc_str):
        return ErrorCode.CLIENT_SESSION_CORRUPT
    if "phone" in exc_str and ("invalid" in exc_str or "error" in exc_str):
        return ErrorCode.CLIENT_PHONE_INVALID
    if "code" in exc_str and ("invalid" in exc_str or "error" in exc_str):
        return ErrorCode.CLIENT_CODE_INVALID
    if "2fa" in exc_str or "two-factor" in exc_str or "password" in exc_str:
        return ErrorCode.CLIENT_2FA_REQUIRED
    
    # Default
    return ErrorCode.INT_UNEXPECTED_ERROR


def get_user_message(code: ErrorCode, **kwargs: Any) -> str:
    """Get user-facing message for an error code (Russian).
    
    Supports template variables like {seconds} for flood wait times.
    """
    msg = _USER_MESSAGES.get(code, "Произошла ошибка. Попробуйте позже.")
    try:
        return msg.format(**kwargs)
    except (KeyError, IndexError):
        return msg


def get_error_message(code: ErrorCode, **kwargs: Any) -> str:
    """Get technical error message for logging (English)."""
    msg = _TECHNICAL_MESSAGES.get(code, "Unknown error")
    try:
        return msg.format(**kwargs)
    except (KeyError, IndexError):
        return msg


def get_error_code(exc: Exception, **kwargs: Any) -> tuple[ErrorCode, str, str]:
    """Get error code, technical message, and user message from exception.
    
    Returns:
        (error_code, technical_message, user_message)
    """
    code = from_exception(exc)
    tech_msg = get_error_message(code, **kwargs)
    user_msg = get_user_message(code, **kwargs)
    return code, tech_msg, user_msg


class AppError(Exception):
    """Application error with error code and context.
    
    Usage:
        raise AppError(ErrorCode.AUTH_FAILED, user_id=12345, details="Invalid token")
    """
    
    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        user_id: int | None = None,
        details: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self.code = code
        self.user_id = user_id
        self.details = details or {}
        self.kwargs = kwargs
        
        tech_msg = message or get_error_message(code, **kwargs)
        super().__init__(tech_msg)
    
    @property
    def user_message(self) -> str:
        """Get user-facing message."""
        return get_user_message(self.code, **self.kwargs)
    
    @property
    def error_id(self) -> str:
        """Get unique error ID for debugging."""
        return f"{self.code.value}_{id(self):x}"
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for logging/serialization."""
        return {
            "error_code": self.code.value,
            "message": str(self),
            "user_message": self.user_message,
            "user_id": self.user_id,
            "details": self.details,
            "error_id": self.error_id,
        }
