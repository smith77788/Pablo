"""
Security middleware and utilities for Infragram API.

Provides:
- Rate limiting (per-user and per-IP)
- Input validation and sanitization
- SQL injection prevention helpers
- XSS prevention (HTML escaping)
- CSRF token generation and validation
- Centralized auth check decorator

All functions are Python 3.12 compatible.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from functools import wraps
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from aiohttp import web

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

_CSRF_SECRET = os.getenv("CSRF_SECRET", "") or os.getenv("WEBHOOK_SECRET", "")
_RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in ("true", "1", "yes")
_RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "120"))
_RATE_LIMIT_AUTH_MAX = int(os.getenv("RATE_LIMIT_AUTH_MAX", "30"))

# ── Rate Limiter ───────────────────────────────────────────────────────────────

class _RateLimiter:
    """In-memory sliding-window rate limiter with IP fallback."""

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, max_requests: int, window: int) -> bool:
        """Return True if allowed, False if rate-limited."""
        if not _RATE_LIMIT_ENABLED:
            return True
        async with self._lock:
            now = time.time()
            cutoff = now - window
            reqs = self._requests.setdefault(key, [])
            self._requests[key] = [t for t in reqs if t > cutoff]
            if len(self._requests[key]) >= max_requests:
                return False
            self._requests[key].append(now)
            return True

    def get_retry_after(self, key: str, window: int) -> int:
        """Return seconds until the oldest request in window expires."""
        reqs = self._requests.get(key, [])
        if not reqs:
            return 0
        oldest = min(reqs)
        return max(1, int(oldest + window - time.time()) + 1)

_rate_limiter = _RateLimiter()


def _client_ip(request: web.Request) -> str:
    """Extract client IP from request (supports X-Forwarded-For)."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-Ip", "")
    if real_ip:
        return real_ip.strip()
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if peername:
        return peername[0]
    return "unknown"


def _user_key(request: web.Request, uid: Optional[int] = None) -> str:
    """Build a rate-limit key from user ID or falling back to IP."""
    if uid:
        return f"u:{uid}"
    return f"ip:{_client_ip(request)}"


async def check_rate_limit(
    request: web.Request,
    uid: Optional[int] = None,
    max_requests: int = _RATE_LIMIT_MAX_REQUESTS,
    window: int = _RATE_LIMIT_WINDOW,
) -> bool:
    """Check rate limit. Returns True if allowed."""
    key = _user_key(request, uid)
    return await _rate_limiter.check(key, max_requests, window)


def rate_limit_response(request: web.Request, uid: Optional[int] = None) -> web.Response:
    """Return a 429 Too Many Requests response."""
    key = _user_key(request, uid)
    retry_after = _rate_limiter.get_retry_after(key, _RATE_LIMIT_WINDOW)
    return web.json_response(
        {"error": "Rate limit exceeded", "retry_after": retry_after},
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


# ── Input Validation ──────────────────────────────────────────────────────────

def validate_integer(value: Any, min_val: int = 0, max_val: int = 2**31 - 1) -> Optional[int]:
    """Validate and return an integer within bounds, or None."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < min_val or v > max_val:
        return None
    return v


def validate_string(value: Any, max_len: int = 1000, required: bool = True) -> Optional[str]:
    """Validate and return a safe string, or None if invalid."""
    if value is None:
        return None if not required else None
    s = str(value).strip()
    if not s and required:
        return None
    return s[:max_len]


def validate_url(url: str, require_https: bool = True) -> bool:
    """Validate a URL is well-formed and optionally requires HTTPS."""
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if require_https and p.scheme != "https":
        return False
    if not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "0.0.0.0"):
        return False
    return True


def validate_email(email: str) -> bool:
    """Basic email validation."""
    if not email or not isinstance(email, str):
        return False
    return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email.strip()))


def sanitize_input(value: Any, max_len: int = 1000) -> str:
    """General-purpose input sanitizer.

    Strips control characters, normalizes whitespace, escapes HTML,
    and limits length. Returns empty string for None/empty input.
    """
    if value is None:
        return ""
    s = str(value).strip()
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)
    s = re.sub(r'\s+', ' ', s)
    s = html.escape(s)
    return s[:max_len]


def validate_phone(phone: str) -> bool:
    """Validate international phone number format.

    Accepts +<country><number> with 10-15 digits total.
    Example: +14155552671, +79161234567
    """
    if not phone or not isinstance(phone, str):
        return False
    cleaned = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    return bool(re.match(r'^\+\d{10,15}$', cleaned))


def sanitize_search_query(query: str, max_len: int = 200) -> str:
    """Sanitize a search query by stripping dangerous chars and limiting length."""
    s = str(query or "").strip()[:max_len]
    return re.sub(r'[<>"\'\\;{}]', '', s)


def validate_bot_token(token: str) -> bool:
    """Validate Telegram bot token format: NNNNNNNNNN:XXXXXXXXXX..."""
    if not token or not isinstance(token, str):
        return False
    return bool(re.match(r'^\d+:[A-Za-z0-9_-]{30,}$', token.strip()))


def validate_username(username: str) -> bool:
    """Validate Telegram username: alphanumeric + underscore, 5-32 chars."""
    if not username:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_]{5,32}$', username.strip().lstrip('@')))


def validate_start_param(param: str) -> bool:
    """Validate deeplink start_param: alphanumeric + _ + -, max 64 chars."""
    if not param:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_-]{1,64}$', param.strip()))


# ── SQL Injection Prevention ───────────────────────────────────────────────────

# Characters that are dangerous in SQL when not parameterized.
_SQL_INJECTION_CHARS = re.compile(r"['\";\\]|--|/\*|\*/|xp_|sp_|UNION|SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE", re.IGNORECASE)


def check_sql_suspicious(value: str) -> bool:
    """Return True if value contains suspicious SQL patterns.

    Note: This is adefense-in-depth check. Primary protection is parameterized queries.
    """
    if not value:
        return False
    return bool(_SQL_INJECTION_CHARS.search(value))


def sanitize_identifier(value: str, max_len: int = 64) -> Optional[str]:
    """Sanitize a string intended for use as a DB identifier (table/column name).

    Only allows alphanumeric + underscore. Returns None if invalid.
    """
    s = str(value or "").strip()[:max_len]
    if not s or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', s):
        return None
    return s


# ── XSS Prevention ────────────────────────────────────────────────────────────

def escape_html(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text or ""))


def escape_json_value(value: Any) -> Any:
    """Escape HTML entities in string values of a dict/list for JSON responses."""
    if isinstance(value, str):
        return escape_html(value)
    if isinstance(value, dict):
        return {k: escape_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [escape_json_value(item) for item in value]
    return value


def safe_json_response(data: Any, status: int = 200, **kwargs) -> web.Response:
    """Return a JSON response with HTML-escaped string values to prevent XSS."""
    return web.json_response(
        escape_json_value(data),
        status=status,
        **kwargs,
    )


# ── CSRF Protection ───────────────────────────────────────────────────────────

def generate_csrf_token(user_id: int) -> str:
    """Generate a CSRF token for a user."""
    if not _CSRF_SECRET:
        return ""
    ts = int(time.time())
    payload = f"{user_id}:{ts}"
    sig = hmac.new(_CSRF_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}:{sig}"


def verify_csrf_token(token: str, user_id: int, max_age: int = 3600) -> bool:
    """Verify a CSRF token. Returns True if valid."""
    if not _CSRF_SECRET or not token:
        return False
    try:
        ts_s, sig = token.split(":", 1)
        ts = int(ts_s)
    except (ValueError, TypeError):
        return False
    if time.time() - ts > max_age:
        return False
    payload = f"{user_id}:{ts}"
    expected = hmac.new(_CSRF_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


def require_csrf(request: web.Request, user_id: int) -> Optional[web.Response]:
    """Check CSRF token from header or body. Returns error response if invalid, None if OK."""
    if not _CSRF_SECRET:
        return None
    token = (
        request.headers.get("X-CSRF-Token", "")
        or request.headers.get("X-XSRF-Token", "")
    )
    if not verify_csrf_token(token, user_id):
        return web.json_response({"error": "Invalid CSRF token"}, status=403)
    return None


# ── Authentication Middleware ──────────────────────────────────────────────────

def require_auth(handler: Callable) -> Callable:
    """Decorator: require valid Bearer token for the handler.

    Attaches uid to request['uid'] on success.
    Returns 401 if authentication fails.
    """
    @wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        from services.mini_app_api import _get_uid
        uid = _get_uid(request)
        if not uid:
            return web.json_response({"error": "Unauthorized"}, status=401)
        request["uid"] = uid
        return await handler(request)
    return wrapped


def require_admin(handler: Callable) -> Callable:
    """Decorator: require admin privileges for the handler."""
    @wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        from services.mini_app_api import _get_uid, _is_admin
        uid = _get_uid(request)
        if not uid:
            return web.json_response({"error": "Unauthorized"}, status=401)
        if not _is_admin(uid):
            return web.json_response({"error": "Forbidden"}, status=403)
        request["uid"] = uid
        return await handler(request)
    return wrapped


def require_rest_api_auth(handler: Callable) -> Callable:
    """Decorator: require X-Api-Key or Bearer token for REST API endpoints."""
    @wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        from config import ADMIN_SECRET
        if ADMIN_SECRET is None:
            return await handler(request)
        if not ADMIN_SECRET:
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = (
            request.headers.get("X-Api-Key", "")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        if not hmac.compare_digest(key, ADMIN_SECRET):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)
    return wrapped


# ── Combined Security Middleware ───────────────────────────────────────────────

def security_middleware() -> Callable:
    """Aiohttp middleware that adds rate limiting and security headers to all responses."""

    @web.middleware
    async def middleware(request: web.Request, handler: Callable) -> web.Response:
        # Rate limit all API requests
        if request.path.startswith("/api/") or request.path.startswith("/webhook"):
            uid = None
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                from services.mini_app_api import _get_uid
                uid = _get_uid(request)
            if not await check_rate_limit(request, uid):
                return rate_limit_response(request, uid)

        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            # Необработанное исключение в хендлере: без этого aiohttp вернёт
            # HTML-страницу 500 с трейсбеком, а мини-апп ждёт JSON и спотыкается
            # на парсинге — пользователь видит «сломанную» ошибку вместо
            # понятного сообщения. Логируем с трейсом и отдаём чистый JSON.
            log.exception("unhandled error in handler for %s %s", request.method, request.path)
            if request.path.startswith("/api/"):
                return web.Response(
                    text='{"error": "Внутренняя ошибка сервера. Попробуйте ещё раз."}',
                    content_type="application/json",
                    status=500,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization",
                    },
                )
            raise

        # Add security headers
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

        # CSP for HTML responses
        ct = response.headers.get("Content-Type", "")
        if "text/html" in ct:
            response.headers.setdefault(
                "Content-Security-Policy",
                # img-src ОБЯЗАТЕЛЬНО перечисляет data: и blob: — без них QR-код
                # входа в аккаунт (мини-апп получает его как data:image/png;base64
                # и вставляет в <img>) молча блокируется браузером: код есть, но
                # НЕ ВИДЕН. Без явного img-src он наследует default-src 'self', а
                # 'self' не покрывает схему data:. blob: — для картинок из
                # createObjectURL. connect-src 'self' — фетчи к нашему же /api.
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob:; "
                "connect-src 'self'"
            )

        return response

    return middleware
