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

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

_CSRF_SECRET = os.getenv("CSRF_SECRET", "") or os.getenv("WEBHOOK_SECRET", "")
_RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() in ("true", "1", "yes")
_RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "120"))
_RATE_LIMIT_AUTH_MAX = int(os.getenv("RATE_LIMIT_AUTH_MAX", "30"))
# Сколько ДОВЕРЕННЫХ прокси стоит перед приложением. X-Forwarded-For дописывает
# каждый прокси справа, а клиент волен прислать сколько угодно значений слева —
# поэтому реальный адрес берём N-м С КОНЦА, где N — число своих прокси
# (Railway/Cloudflare — обычно 1). 0 отключает доверие к заголовку целиком.
_TRUSTED_PROXY_HOPS = max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "1") or 0))
# Потолок числа ключей в лимитере: ключ — это user id ИЛИ адрес клиента, а
# адресов может быть сколько угодно. Без потолка словарь растёт бесконечно.
_RATE_LIMIT_MAX_KEYS = max(1000, int(os.getenv("RATE_LIMIT_MAX_KEYS", "20000") or 20000))

# ── Rate Limiter ───────────────────────────────────────────────────────────────

class _RateLimiter:
    """In-memory sliding-window rate limiter with IP fallback.

    Записи чистятся не только при обращении к тому же ключу: ключ — это адрес
    клиента, и адресов может быть сколько угодно. Раньше ключ, к которому
    больше не обращались, оставался в словаре навсегда — поток запросов с
    меняющихся адресов надувал память процесса без ограничений. Теперь есть
    периодическая вычистка протухших окон и жёсткий потолок числа ключей.
    """

    def __init__(self) -> None:
        self._requests: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._last_sweep = 0.0

    def _sweep(self, now: float, window: int) -> None:
        """Выбросить ключи, у которых в окне не осталось ни одного запроса."""
        cutoff = now - window
        for k in [k for k, ts in self._requests.items()
                  if not ts or ts[-1] <= cutoff]:
            self._requests.pop(k, None)
        if len(self._requests) > _RATE_LIMIT_MAX_KEYS:
            # Всё ещё тесно: жертвуем самыми давними — у них меньше всего шансов
            # оказаться живой сессией, а лимит для них просто начнётся заново.
            victims = sorted(self._requests.items(), key=lambda kv: kv[1][-1])
            for k, _ in victims[: len(self._requests) - _RATE_LIMIT_MAX_KEYS]:
                self._requests.pop(k, None)

    async def check(self, key: str, max_requests: int, window: int) -> bool:
        """Return True if allowed, False if rate-limited."""
        if not _RATE_LIMIT_ENABLED:
            return True
        async with self._lock:
            now = time.time()
            cutoff = now - window
            # Вычистка не чаще раза в окно — стоимость размазана по запросам.
            if now - self._last_sweep >= window:
                self._last_sweep = now
                self._sweep(now, window)
            reqs = self._requests.setdefault(key, [])
            kept = [t for t in reqs if t > cutoff]
            if len(kept) >= max_requests:
                self._requests[key] = kept
                return False
            kept.append(now)
            self._requests[key] = kept
            return True

    def get_count(self, key: str, window: int) -> int:
        """Сколько отметок у ключа в окне (без записи новой)."""
        cutoff = time.time() - window
        return sum(1 for t in self._requests.get(key, ()) if t > cutoff)

    def get_retry_after(self, key: str, window: int) -> int:
        """Return seconds until the oldest request in window expires."""
        reqs = self._requests.get(key, [])
        if not reqs:
            return 0
        oldest = min(reqs)
        return max(1, int(oldest + window - time.time()) + 1)

_rate_limiter = _RateLimiter()


def _peer_ip(request: web.Request) -> str:
    """Адрес, с которого реально пришло соединение (подделать нельзя)."""
    try:
        peername = request.transport.get_extra_info("peername") if request.transport else None
    except Exception:
        peername = None
    if peername:
        return str(peername[0])
    return "unknown"


def _client_ip(request: web.Request) -> str:
    """Адрес клиента для лимитера: X-Forwarded-For, но только доверенная часть.

    Заголовок дописывается СПРАВА каждым прокси, а слева клиент может написать
    что угодно. Раньше брали самое левое значение — то есть ровно то, что
    полностью контролирует клиент. Любой мог слать новый X-Forwarded-For на
    каждый запрос, получая каждый раз чистый лимит: ограничение частоты для
    всех незалогиненных маршрутов (вход, обмен кода связывания) фактически не
    работало, а словарь лимитера рос с каждым выдуманным адресом.

    Теперь берём N-е значение С КОНЦА, где N — число собственных прокси
    (TRUSTED_PROXY_HOPS, по умолчанию 1: Railway). Если заголовка нет, короче
    ожидаемого или значение не похоже на адрес — откатываемся на адрес
    соединения. При TRUSTED_PROXY_HOPS=0 заголовок игнорируется целиком.
    """
    if _TRUSTED_PROXY_HOPS <= 0:
        return _peer_ip(request)
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= _TRUSTED_PROXY_HOPS:
            cand = parts[-_TRUSTED_PROXY_HOPS]
            if _looks_like_ip(cand):
                return cand
    # X-Real-Ip сюда НЕ берём: его ставит только nginx, которого перед нами
    # нет, а без прокси заголовок пишет сам клиент — новый адрес на каждый
    # запрос давал чистый лимит (перебор кода связывания, входа, REST-ключа).
    return _peer_ip(request)


_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _looks_like_ip(value: str) -> bool:
    """Грубая проверка формата адреса: мусор в ключ лимитера не пускаем."""
    if not value or len(value) > 45:
        return False
    if _IPV4_RE.match(value):
        return all(0 <= int(p) <= 255 for p in value.split("."))
    # IPv6: допускаем только hex-группы, двоеточия и зону/встроенный IPv4.
    return bool(re.fullmatch(r"[0-9A-Fa-f:.%]+", value)) and ":" in value


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


# Маршруты входа и связывания устройства: здесь перебирают коды и initData, а не
# пользуются приложением. Лимит для них строже общего (RATE_LIMIT_AUTH_MAX).
AUTH_RATE_LIMIT_PATHS = (
    "/api/miniapp/auth",
    "/api/miniapp/pair",
    "/api/miniapp/pair/exchange",
)


def _rate_key(request: web.Request, uid: Optional[int] = None) -> str:
    """Ключ лимитера: у входа он свой, с приставкой auth:.

    Счётчик отдельный намеренно. С общим счётчиком обычный трафик приложения
    (дашборд, список аккаунтов, поток событий — десятки запросов в минуту) съедал
    бы лимит входа, а перебор кодов связывания, наоборот, выбивал бы из
    приложения самого владельца.
    """
    key = _user_key(request, uid)
    if request.path in AUTH_RATE_LIMIT_PATHS:
        return "auth:" + key
    return key


async def check_rate_limit_for_path(
    request: web.Request, uid: Optional[int] = None
) -> bool:
    """Лимит с учётом маршрута: вход и связывание — по RATE_LIMIT_AUTH_MAX.

    RATE_LIMIT_AUTH_MAX был объявлен и настраивался переменной окружения, но не
    использовался нигде: вход жил под общим лимитом в 120 запросов в минуту.
    """
    if request.path in AUTH_RATE_LIMIT_PATHS:
        return await _rate_limiter.check(
            _rate_key(request, uid), _RATE_LIMIT_AUTH_MAX, _RATE_LIMIT_WINDOW
        )
    return await _rate_limiter.check(
        _rate_key(request, uid), _RATE_LIMIT_MAX_REQUESTS, _RATE_LIMIT_WINDOW
    )


def rate_limit_response(request: web.Request, uid: Optional[int] = None) -> web.Response:
    """Return a 429 Too Many Requests response."""
    key = _rate_key(request, uid)
    retry_after = _rate_limiter.get_retry_after(key, _RATE_LIMIT_WINDOW)
    # Текст по-русски: это сообщение видит владелец в мини-аппе, а английского
    # он не читает. Поле error оставлено тем же (его читает фронтенд), поменялось
    # только значение.
    return web.json_response(
        {"error": f"Слишком часто — подождите {retry_after} с и повторите",
         "retry_after": retry_after},
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


# Идентификаторы Telegram — 64-битные со знаком: у групп и каналов они
# отрицательные (-100…), у пользователей и ботов давно больше 2**31
# (боты, созданные с 2023 года, — десятизначные 6…/7…/8…). Граница
# validate_integer по умолчанию (0 … 2**31-1) такие id молча отбрасывает,
# и ручка отвечает «нужен идентификатор», хотя он передан.
TG_ID_MIN = -(2 ** 63)
TG_ID_MAX = 2 ** 63 - 1


def tg_id(value: Any, *, positive: bool = False) -> Optional[int]:
    """id из Telegram (чат, пользователь, бот, канал) или None.

    Ноль id не бывает — это «не передан». `positive=True` — для id, которые
    отрицательными не бывают (бот, пользователь, владелец).
    """
    v = validate_integer(value, min_val=1 if positive else TG_ID_MIN, max_val=TG_ID_MAX)
    return v if v else None


def selection_ids(
    body: Any, key: str, *, what: str = "объект", cap: Optional[int] = None,
) -> "tuple[Optional[list[int]], Optional[str]]":
    """Выбор целей массового действия из тела запроса: (ids | None=все, ошибка).

    Три случая, и смешивать их нельзя:

    * ключа нет (или он null) — осознанное «все»: экран, который шлёт во все,
      ключ НЕ передаёт;
    * список передан, но пуст или с мусором — это ОШИБКА выбора, а не «все».
      Прежняя форма `[int(x) for x in (body.get(key) or []) if ...]` превращала
      `[]` и `["abc"]` в пустой список, а пустой список — в «все каналы/боты»:
      правка одного бота уходила на весь парк, пост — во все каналы;
    * иначе — список целых без повторов, в исходном порядке.
    """
    raw = body.get(key) if isinstance(body, dict) else None
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not raw:
        return None, f"Не выбран ни один {what}"
    out: list[int] = []
    seen: set[int] = set()
    for x in raw:
        if isinstance(x, bool):
            return None, f"Неверный идентификатор: {what}"
        try:
            v = int(str(x).strip())
        except (TypeError, ValueError):
            return None, f"Неверный идентификатор: {what}"
        if v not in seen:
            seen.add(v)
            out.append(v)
    if cap is not None and len(out) > cap:
        return None, f"Слишком много выбрано: не больше {cap} за раз"
    return out, None


# ── SSRF-гард ────────────────────────────────────────────────────────────────
# Гард жил ДВАЖДЫ: синтаксическая проверка по регуляркам внутри mini_app_api и
# усиленная версия в ветке аудита, которая так и не была влита. Работала первая —
# и пропускала канонический IPv6: `https://[::ffff:127.0.0.1]/` (IPv4 в
# IPv6-обёртке) и ULA/link-local `fc00::/fe80::` не совпадали ни с одной её
# регуляркой. Проверка через ipaddress закрывает весь класс разом, а резолв DNS —
# имена, указывающие внутрь, и нестандартные записи IPv4 (`https://2130706433/`,
# `https://127.1/`, `https://0x7f000001/`).
# Одна реализация на проект, в модуле безопасности.


def is_internal_ip(ip_str: str) -> bool:
    """True, если IP приватный/служебный (loopback, private, link-local,
    reserved, multicast, unspecified). Неразбираемое — считаем небезопасным.

    IPv4-mapped IPv6 (``::ffff:127.0.0.1``) разворачиваем в IPv4 перед проверкой.
    """
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def is_safe_public_url(url: str) -> bool:
    """SSRF-гард (синтаксический слой): https + hostname не является внутренним
    литерал-адресом. НЕ ловит имена, резолвящиеся внутрь, и нестандартные
    кодировки IPv4 — для реального fetch используйте ``resolve_url_is_public``.
    Чистая функция — тестируема.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname.lower()
    if host in ("localhost", "0.0.0.0") or host.endswith(".local") or host.endswith(".internal"):
        return False
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True  # обычное имя — резолв проверит async-слой
    return not is_internal_ip(host)


async def resolve_url_is_public(url: str) -> bool:
    """Авторитетный SSRF-гард: синтаксическая проверка + РЕЗОЛВ DNS каждого
    адреса. Отвергает, если ХОТЯ БЫ ОДИН резолвнутый IP внутренний. Закрывает
    нестандартные кодировки IPv4 (резолвятся в 127.0.0.1) и имена, указывающие
    внутрь (metadata через DNS). Остаётся узкое окно DNS-rebinding до connect,
    но http-схема (нужная большинству metadata-эндпоинтов) запрещена.
    """
    import socket
    if not is_safe_public_url(url):
        return False
    try:
        host = urlparse(url.strip()).hostname
    except Exception:
        return False
    if not host:
        return False
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    return not any(is_internal_ip(a) for a in addrs)


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
        # Ключ не настроен — доступа нет. Ветки «секрет is None — пускаем всех»
        # здесь больше нет: см. services/rest_api._check_auth.
        from services.rest_api import rest_secret_strong
        if not ADMIN_SECRET or not rest_secret_strong(ADMIN_SECRET):
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = (
            request.headers.get("X-Api-Key", "")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        # Байты, а не строки: см. services/rest_api._check_auth.
        if not hmac.compare_digest(key.encode("utf-8"), ADMIN_SECRET.encode("utf-8")):
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
            # Трассировка (аудит №6): каждый запрос бежит в своей задаче, контекст
            # корреляции изолирован. Проставляем cid (+ user_id, если узнали) в
            # начале — тогда ЛЮБАЯ строка лога, рождённая при обработке запроса,
            # привязана к запросу и пользователю. Раньше корреляцию выставлял
            # только апдейт-миддлварь бота; мини-апп — главная веб-поверхность —
            # логировала без контекста, и аварию приходилось собирать вручную.
            try:
                from services.logger import (
                    set_correlation_id, generate_correlation_id)
                set_correlation_id(
                    correlation_id=generate_correlation_id(), user_id=uid)
            except Exception:
                pass
            if not await check_rate_limit_for_path(request, uid):
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
        ct = response.headers.get("Content-Type", "")
        is_html = "text/html" in ct
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Страницу мини-аппа Telegram Web (web.telegram.org) открывает во
        # фрейме, и X-Frame-Options: DENY её там просто не показывал. Для HTML
        # от кликджекинга защищает frame-ancestors ниже — со списком Telegram;
        # остальным ответам фрейм не нужен вовсе.
        if not is_html:
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Только HTTPS на год: без него первый заход по http:// (ссылка,
        # набранный адрес) перехватывается и уводится посредником.
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")

        # CSP for HTML responses
        if is_html:
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
                "connect-src 'self'; "
                # Без плагинов, без подмены базового адреса ссылок через
                # внедрённый <base>, формы — только к себе.
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            )

        return response

    return middleware


# ── Секрет главного вебхука бота ───────────────────────────────────────────────
#
# Главный бот на вебхуке (WEBHOOK_URL) принимал POST /webhook от кого угодно:
# ни setWebhook, ни обработчик aiogram не получали secret_token. Апдейт
# Telegram — это просто JSON, и поле from.id в нём пишет отправитель. Значит,
# любой, кто знал адрес сервера, мог прислать «сообщение от админа» (/admin,
# правка переменных окружения, бэкап базы) или «нажатие кнопки» от имени
# любого владельца — бот исполнял его как настоящее.
#
# Telegram умеет класть секрет в заголовок X-Telegram-Bot-Api-Secret-Token
# каждого апдейта. Секрет выводим из токена бота (HMAC с меткой), поэтому
# хранить и настраивать его не нужно: обе стороны считают одно и то же.
# Метка отличает его от ключа подписи сессий мини-аппа (sha256(token)) —
# утечка одного не даёт второго. TELEGRAM_WEBHOOK_SECRET задаёт его явно.

_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def main_webhook_secret(bot_token: str) -> str:
    """Секрет заголовка X-Telegram-Bot-Api-Secret-Token для главного бота.

    Пустой/негодный токен — ValueError: вебхук без секрета не ставим вовсе.
    """
    explicit = (os.getenv("TELEGRAM_WEBHOOK_SECRET", "") or "").strip()
    if explicit:
        if not _WEBHOOK_SECRET_RE.match(explicit) or len(explicit) < 16:
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET: нужно 16–256 символов A-Z a-z 0-9 _ -")
        return explicit
    if not bot_token or len(bot_token.strip()) < 16:
        raise ValueError("нет токена бота — секрет вебхука не из чего вывести")
    return hmac.new(bot_token.encode("utf-8"), b"tg-main-webhook-secret",
                    hashlib.sha256).hexdigest()


# ── Проверка TLS у исходящих запросов ──────────────────────────────────────────
#
# Сессия бота и общий HTTP-клиент процесса ходили наружу с выключенной
# проверкой сертификата (ssl=False / CERT_NONE). Токен бота едет в КАЖДОМ
# адресе Bot API (api.telegram.org/bot<токен>/…), токены подключённых ботов и
# ключи ИИ-провайдеров — тоже через этот клиент. Без проверки сертификата
# любой посредник на пути (провайдер, чужой прокси, подменённый DNS)
# представляется api.telegram.org и забирает токены целиком.
#
# Теперь проверка включена всегда, с корневыми сертификатами certifi.
# OUTBOUND_TLS_VERIFY=0 — аварийный выключатель на случай, если у какого-то
# окружения сломаны корневые сертификаты: громкая запись в лог, не тихо.

def outbound_tls_verify_enabled() -> bool:
    return (os.getenv("OUTBOUND_TLS_VERIFY", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def outbound_ssl_context():
    """SSL-контекст для исходящих запросов: проверка сертификата и имени."""
    import ssl
    if not outbound_tls_verify_enabled():
        log.error(
            "OUTBOUND_TLS_VERIFY=0: проверка сертификатов исходящих запросов "
            "ВЫКЛЮЧЕНА — токены уязвимы для перехвата. Верните 1 как можно скорее.")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ── Скачивание по адресу пользователя (SSRF) ───────────────────────────────────
#
# resolve_url_is_public проверяет адрес ДО запроса, но запрос потом резолвит
# имя ЗАНОВО и ходит по переадресациям сам. Отсюда два обхода, оба рабочие:
#   * переадресация: публичная страница отвечает 302 на
#     http://169.254.169.254/… (метаданные облака) или http://127.0.0.1:5432 —
#     клиент послушно идёт туда;
#   * DNS-rebinding: имя с TTL 0 отдаёт публичный адрес проверке и внутренний
#     самому запросу.
# Поэтому проверка живёт в соединителе: КАЖДОЕ соединение (и после каждой
# переадресации, и для адреса-литерала) идёт только на публичный IP.

class InternalAddressBlocked(OSError):
    """Соединение во внутреннюю сеть запрещено."""


class PublicOnlyConnector(aiohttp.TCPConnector):
    async def _resolve_host(self, host, port, traces=None):  # type: ignore[override]
        hosts = await super()._resolve_host(host, port, traces=traces)
        if not hosts or any(is_internal_ip(h.get("host", "")) for h in hosts):
            raise InternalAddressBlocked(f"адрес {host} ведёт во внутреннюю сеть")
        return hosts


def public_http_session(**kwargs):
    """ClientSession, который физически не соединяется с внутренними адресами.

    Для любых запросов по адресу, пришедшему от пользователя (картинка, медиа,
    вебхук). trust_env=False: переменные прокси окружения не уводят запрос в
    обход проверки.
    """
    kwargs.setdefault("connector", PublicOnlyConnector(ssl=outbound_ssl_context()))
    kwargs.setdefault("trust_env", False)
    return aiohttp.ClientSession(**kwargs)


async def read_capped(resp, max_bytes: int) -> bytes:
    """Прочитать тело не больше max_bytes (+1 байт — признак превышения).

    resp.read() тянет в память всё, что пришлёт сервер: «картинка» на 10 ГБ
    валила процесс раньше, чем срабатывала проверка размера.
    """
    data = await resp.content.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Файл больше {max_bytes // (1024 * 1024)} МБ")
    return data


class _Borrowed:
    """Чужая сессия как контекст, который её НЕ закрывает."""

    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


def guarded_session(http):
    """Сессия для запроса по адресу пользователя.

    Настоящая aiohttp-сессия без PublicOnlyConnector подменяется своей
    защищённой (закроется по выходе). Уже защищённая — и подставные объекты
    тестов — используются как есть.
    """
    if isinstance(http, aiohttp.ClientSession) and not isinstance(
            http.connector, PublicOnlyConnector):
        return public_http_session()
    return _Borrowed(http)


# ── Выгрузки CSV без формул (CSV/formula injection) ───────────────────────────
#
# В выгрузки попадают имена, био, юзернеймы и тексты из Telegram — то, что
# пишет посторонний человек. Ячейка «=HYPERLINK("https://…?"&A2;"Открыть")»
# или «=cmd|' /C calc'!A0» в имени участника становится в Excel/Google
# Таблицах формулой: утекают соседние ячейки (телефоны), запускаются команды.
# Ячейку, начинающуюся с = + - @ (а также таб/CR), экранируем апострофом —
# так её показывают как текст. Номер телефона «+7 999 …» и число «-5» формулой
# не являются и остаются как есть.

_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CSV_PLAIN_NUMBER = re.compile(r"^[+-]?[\d\s().,-]+$")


def csv_safe_cell(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith(_CSV_FORMULA_START):
        return value
    if _CSV_PLAIN_NUMBER.match(value):
        return value
    return "'" + value


class _SafeCsvWriter:
    def __init__(self, inner):
        self._w = inner

    def writerow(self, row):
        if isinstance(row, dict):
            return self._w.writerow({k: csv_safe_cell(v) for k, v in row.items()})
        return self._w.writerow([csv_safe_cell(c) for c in row])

    def writerows(self, rows):
        for r in rows:
            self.writerow(r)

    def __getattr__(self, name):
        return getattr(self._w, name)


def safe_csv_writer(f, *args, **kwargs):
    """csv.writer, который не пропускает формулы из чужого текста."""
    import csv
    return _SafeCsvWriter(csv.writer(f, *args, **kwargs))


def safe_csv_dict_writer(f, *args, **kwargs):
    """csv.DictWriter с той же защитой (writeheader — как у DictWriter)."""
    import csv
    return _SafeCsvWriter(csv.DictWriter(f, *args, **kwargs))
