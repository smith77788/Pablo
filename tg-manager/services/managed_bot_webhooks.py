"""Подключённые боты получают обновления вебхуком, а не опросом.

ЧТО БЫЛО. Каждый подключённый бот опрашивался `getUpdates` раз в 10 секунд
(services/auto_responder.py), «горячие» — раз в секунду. Человек нажимал
кнопку или писал боту и ждал ответа до 10 секунд; на тысячах ботов опрос —
это тысячи пустых запросов к Telegram каждые 10 секунд. Модуль вебхуков
здесь уже был, но наполовину: регистрацию никто не вызывал, очередь
обновлений никто не читал, а обработчик находил бота по первой попавшейся
очереди.

КАК ТЕПЕРЬ.
  * Адрес бота — `{база}/tgbot/hook/{bot_id}`, секрет — sha256 от токена
    бота (Telegram присылает его в X-Telegram-Bot-Api-Secret-Token). Чужой
    запрос без верного секрета получает 401 и ничего не трогает.
  * Обновление разбирает тот же `auto_responder._process_bot`, что и опрос:
    одна логика, один оффсет. Повтор того же обновления (Telegram повторяет,
    если не дождался ответа) отсекается по оффсету; `max_connections=1`
    держит обновления бота строго по порядку, как и опрос.
  * `sync` (раз в ~5 минут из цикла опроса): ставит вебхук активным ботам,
    помечает их `use_webhook`, и опрос их больше не трогает. Если Telegram
    не может достучаться до нас (ошибка доставки и копится очередь), бот
    возвращается на опрос и не переводится на вебхук `_RETRY_AFTER`.
  * Включено, когда известен публичный HTTPS-адрес (PUBLIC_BASE_URL или
    адрес вебхука главного бота WEBHOOK_URL). MANAGED_BOTS_DELIVERY=polling
    возвращает всех ботов на опрос на следующем проходе.
  * Если ни одной из двух переменных нет, адрес берётся из MINI_APP_URL, а
    затем из RAILWAY_PUBLIC_DOMAIN. Раньше без PUBLIC_BASE_URL вебхуки не
    включались вовсе, и все боты клиентов отвечали с задержкой до 10 секунд,
    хотя публичный адрес у сервера был — мини-апп на нём и живёт. Такой
    «угаданный» адрес сначала проверяется (`_guessed_base_is_ours`): на нём
    должен отвечать именно наш приёмник вебхуков. Не отвечает — проход
    сверки пропускается целиком, боты остаются как есть, а не снимаются с
    вебхука из-за сетевого блипа.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

HOOK_PREFIX = "/tgbot/hook/"
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# Бот, до которого Telegram не достучался, возвращается на опрос и не
# переводится обратно столько времени.
_RETRY_AFTER = timedelta(hours=6)
# Ошибка доставки свежее этого и непустая очередь — вебхук не работает.
_ERROR_FRESH = timedelta(minutes=10)
_SYNC_BATCH = 200
# Секрет вебхука — sha256 hex (_make_secret): другой формы он не бывает.
_SECRET_SHAPE = re.compile(r"^[0-9a-f]{64}$")
# Промахов секрета с одного адреса за минуту, после которых — 429 без базы.
_HOOK_FAIL_MAX = 20

# Обновления одного бота разбираются строго по одному: опрос делал так же,
# а оффсет — один на бота. Живёт в процессе, который принимает HTTP.
_locks: dict[int, asyncio.Lock] = {}


def _make_secret(bot_token: str) -> str:
    """Секрет вебхука бота: sha256 от расшифрованного токена (64 hex-символа,
    Telegram разрешает 1–256 символов A-Za-z0-9_-). Считается заново на
    обеих сторонах — хранить его негде и незачем."""
    return hashlib.sha256(bot_token.encode()).hexdigest()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else ""


def _normalize(raw: str) -> str | None:
    raw = (raw or "").strip().rstrip("/")
    if raw.endswith("/miniapp"):
        raw = raw[: -len("/miniapp")]
    return raw if raw.startswith("https://") else None


def base_url() -> str | None:
    """Публичный HTTPS-адрес, ЯВНО заданный для вебхуков, или None."""
    if os.getenv("MANAGED_BOTS_DELIVERY", "").strip().lower() == "polling":
        return None
    raw = (os.getenv("PUBLIC_BASE_URL", "") or "").strip()
    if not raw:
        main_hook = (os.getenv("WEBHOOK_URL", "") or "").strip()
        if main_hook:
            raw = _origin(main_hook)
    return _normalize(raw)


def guessed_base_url() -> str | None:
    """Адрес сервера, выведенный из MINI_APP_URL / RAILWAY_PUBLIC_DOMAIN.

    Только когда явного адреса нет и опрос не включён принудительно. Перед
    использованием его проверяет `_guessed_base_is_ours`: MINI_APP_URL может
    указывать и на чужой хостинг страницы.
    """
    if os.getenv("MANAGED_BOTS_DELIVERY", "").strip().lower() == "polling":
        return None
    if base_url():
        return None
    mini = (os.getenv("MINI_APP_URL", "") or "").strip()
    if mini:
        got = _normalize(_origin(mini))
        if got:
            return got
    domain = (os.getenv("RAILWAY_PUBLIC_DOMAIN", "") or "").strip().strip("/")
    if domain and "/" not in domain and ":" not in domain:
        return _normalize(f"https://{domain}")
    return None


# Итог проверки угаданного адреса: {адрес: (годен, monotonic-до)}.
_probe_cache: dict[str, tuple[bool, float]] = {}
_PROBE_OK_TTL = 3600.0
_PROBE_FAIL_TTL = 600.0


async def _guessed_base_is_ours(http: aiohttp.ClientSession, base: str) -> bool:
    """На адресе отвечает НАШ приёмник вебхуков.

    Стучимся в `/tgbot/hook/0` без секрета: наш обработчик отвечает ровно
    401 «unauthorized», не трогая базу. Любой другой ответ (404 чужого
    хостинга, страница-заглушка) — адрес не наш, вебхуки на него не ставим.
    """
    now = time.monotonic()
    hit = _probe_cache.get(base)
    if hit and hit[1] > now:
        return hit[0]
    ok = False
    try:
        async with http.post(
            f"{base}{HOOK_PREFIX}0",
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=False,
        ) as resp:
            ok = resp.status == 401 and (await resp.text()).strip() == "unauthorized"
    except Exception as e:  # noqa: BLE001 — сеть: просто «не подтверждён»
        log.info("managed_bot_webhooks: проверка адреса %s не прошла: %s",
                 base, type(e).__name__)
    _probe_cache[base] = (ok, now + (_PROBE_OK_TTL if ok else _PROBE_FAIL_TTL))
    if not ok:
        log.warning(
            "managed_bot_webhooks: по адресу %s не отвечает приёмник вебхуков — "
            "боты остаются на опросе. Задайте PUBLIC_BASE_URL, если сервер "
            "доступен по другому адресу.", base)
    return ok


def hook_url(base: str, bot_id: int) -> str:
    return f"{base.rstrip('/')}{HOOK_PREFIX}{int(bot_id)}"


def webhook_broken(info: dict, now: datetime | None = None) -> bool:
    """Вебхук стоит, но Telegram не может доставить: свежая ошибка доставки
    и копящаяся очередь. ЧИСТАЯ."""
    now = now or datetime.now(timezone.utc)
    err_at = info.get("last_error_date")
    if not err_at or not info.get("pending_update_count"):
        return False
    try:
        when = datetime.fromtimestamp(int(err_at), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False
    return now - when <= _ERROR_FRESH


async def register_webhook(
    http: aiohttp.ClientSession,
    bot_token: str,
    bot_id: int,
    base_url: str,
) -> bool:
    """Поставить вебхук одному боту. True — Telegram принял."""
    # Токен приходит прямо из managed_bots.token, а он хранится зашифрованным:
    # шифротекст в адресе Telegram — это 401. Расшифровываем на входе, чтобы
    # секрет и адрес считались от одного вида токена.
    from services import bot_api
    from services.auto_responder import _MANAGED_BOT_ALLOWED_UPDATES
    from services.token_vault import decrypt_token

    bot_token = decrypt_token(bot_token or "")
    secret = _make_secret(bot_token)
    try:
        data = await bot_api._call(
            http, bot_token, "setWebhook",
            url=hook_url(base_url, bot_id), secret_token=secret,
            allowed_updates=_MANAGED_BOT_ALLOWED_UPDATES,
            # Ожидающие обновления не выбрасываем: их доставит вебхук, а
            # повтор уже разобранного отсечёт оффсет.
            drop_pending_updates=False,
            max_connections=1,
        )
    except Exception as e:
        log.warning("managed_bot_webhooks: setWebhook bot=%d: %s", bot_id, e)
        return False
    if not data.get("ok"):
        log.warning("managed_bot_webhooks: setWebhook bot=%d отклонён: %s",
                    bot_id, str(data.get("description"))[:200])
        return False
    return True


async def unregister_webhook(
    http: aiohttp.ClientSession,
    bot_token: str,
    bot_id: int,
) -> bool:
    """Снять вебхук (бот возвращается на опрос). Очередь не выбрасываем."""
    from services import bot_api
    from services.token_vault import decrypt_token

    bot_token = decrypt_token(bot_token or "")
    try:
        data = await bot_api._call(http, bot_token, "deleteWebhook",
                                   drop_pending_updates=False)
    except Exception as e:
        log.warning("managed_bot_webhooks: deleteWebhook bot=%d: %s", bot_id, e)
        return False
    return bool(data.get("ok"))


# ── Сверка: кому вебхук, кому опрос ─────────────────────────────────────────

async def _set_mode(pool, bot_id: int, on: bool, failed: bool = False) -> None:
    await pool.execute(
        """UPDATE managed_bots
              SET use_webhook=$2,
                  webhook_failed_at = CASE WHEN $3 THEN now() ELSE webhook_failed_at END
            WHERE bot_id=$1""", bot_id, on, failed)


async def sync(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> dict:
    """Один проход сверки. {'on': n, 'off': n, 'fallback': n}."""
    from services import bot_api
    from services.token_vault import decrypt_token

    stats = {"on": 0, "off": 0, "fallback": 0}
    base = base_url()
    if not base:
        guessed = guessed_base_url()
        if guessed:
            if not await _guessed_base_is_ours(http, guessed):
                # Не подтвердили — ничего не меняем: снять всех с вебхука из-за
                # сетевого блипа на проверке значило бы замедлить всех ботов.
                return stats
            base = guessed
    if not base:
        rows = await pool.fetch(
            "SELECT bot_id, token FROM managed_bots WHERE COALESCE(use_webhook,FALSE)")
        for r in rows:
            if await unregister_webhook(http, r["token"], r["bot_id"]):
                await _set_mode(pool, r["bot_id"], False)
                stats["off"] += 1
        return stats

    # Боты на вебхуке: Telegram до нас достаёт? Адрес наш?
    live = await pool.fetch(
        """SELECT bot_id, token FROM managed_bots
            WHERE is_active AND COALESCE(use_webhook,FALSE) LIMIT $1""", _SYNC_BATCH)
    for r in live:
        token = decrypt_token(r["token"] or "")
        try:
            info = (await bot_api._call(http, token, "getWebhookInfo")).get("result") or {}
        except Exception:
            continue
        if webhook_broken(info):
            log.warning("managed_bot_webhooks: bot=%d вебхук не доставляется (%s) — "
                        "возвращаю на опрос", r["bot_id"],
                        str(info.get("last_error_message"))[:120])
            if await unregister_webhook(http, r["token"], r["bot_id"]):
                await _set_mode(pool, r["bot_id"], False, failed=True)
                stats["fallback"] += 1
        elif info.get("url") != hook_url(base, r["bot_id"]):
            # Вебхук сняли или поставили чужой — возвращаем свой; не вышло —
            # бот уходит на опрос, а не глохнет.
            if not await register_webhook(http, r["token"], r["bot_id"], base):
                await _set_mode(pool, r["bot_id"], False, failed=True)
                stats["fallback"] += 1

    # Боты на опросе — на вебхук (кроме недавно не доставлявшихся).
    todo = await pool.fetch(
        """SELECT bot_id, token FROM managed_bots
            WHERE is_active AND NOT COALESCE(use_webhook,FALSE)
              AND (webhook_failed_at IS NULL OR webhook_failed_at < now() - $1::interval)
            ORDER BY bot_id LIMIT $2""", _RETRY_AFTER, _SYNC_BATCH)
    for r in todo:
        # Сначала флаг: опрос этого бота прекращается, и его getUpdates не
        # успеет получить 409 и снять только что поставленный вебхук.
        await _set_mode(pool, r["bot_id"], True)
        if await register_webhook(http, r["token"], r["bot_id"], base):
            stats["on"] += 1
        else:
            await _set_mode(pool, r["bot_id"], False, failed=True)
    if any(stats.values()):
        log.info("managed_bot_webhooks: сверка %s", stats)
    return stats


# ── Приём обновлений ────────────────────────────────────────────────────────

def make_webhook_route(pool: asyncpg.Pool, http_getter=None, main_bot=None):
    """Обработчик POST /tgbot/hook/{bot_id}."""
    import json as _json
    from aiohttp import web as _web

    _own_http: dict = {}

    def _http() -> aiohttp.ClientSession:
        sess = http_getter() if http_getter else None
        if sess is not None and not sess.closed:
            return sess
        cur = _own_http.get("s")
        if cur is None or cur.closed:
            cur = _own_http["s"] = aiohttp.ClientSession()
        return cur

    async def handler(request: _web.Request) -> _web.Response:
        try:
            bot_id = int(request.match_info.get("bot_id", ""))
        except (TypeError, ValueError):
            return _web.Response(status=404, text="not found")
        # Адрес открыт всему интернету, а проверка секрета стоит запроса в базу
        # и расшифровки токена. Без заголовка нужного вида (наш секрет — 64
        # hex) отказываем сразу; частые промахи с одного адреса — 429 без базы.
        got = request.headers.get(_SECRET_HEADER, "")
        if not _SECRET_SHAPE.match(got):
            return _web.Response(status=401, text="unauthorized")
        from services.security import _client_ip, _rate_limiter
        fail_key = f"tghook-fail:{_client_ip(request)}"
        if _rate_limiter.get_count(fail_key, 60) >= _HOOK_FAIL_MAX:
            return _web.Response(status=429, text="too many requests")
        from services.token_vault import decrypt_token
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND is_active", bot_id)
        if not row:
            return _web.Response(status=404, text="not found")
        token = decrypt_token(row["token"] or "")
        # Сравнение байтами: compare_digest на строке с не-ASCII символами
        # бросает TypeError, и чужой заголовок давал бы 500 вместо 401.
        if not got or not hmac.compare_digest(
                got.encode("utf-8", "replace"), _make_secret(token).encode()):
            await _rate_limiter.check(fail_key, 10 ** 9, 60)   # учёт промаха
            return _web.Response(status=401, text="unauthorized")
        try:
            update = _json.loads(await request.read())
        except Exception:
            # Нечитаемое тело повтор не исправит — отвечаем 200, чтобы
            # Telegram не долбил им очередь бота.
            return _web.Response(status=200, text="OK")
        if not isinstance(update, dict):
            return _web.Response(status=200, text="OK")
        from services import auto_responder
        lock = _locks.setdefault(bot_id, asyncio.Lock())
        started = time.monotonic()
        async with lock:
            # Ошибки разбора _process_bot глотает и сдвигает оффсет сам
            # (ядовитое обновление не повторяется) — ровно как при опросе.
            await auto_responder._process_bot(
                pool, _http(), bot_id, token, main_bot, updates=[update])
        took = time.monotonic() - started
        if took > 20:
            log.warning("managed_bot_webhooks: bot=%d обновление разбиралось %.0f с",
                        bot_id, took)
        return _web.Response(status=200, text="OK")

    async def _close(_app=None) -> None:
        sess = _own_http.pop("s", None)
        if sess is not None and not sess.closed:
            await sess.close()

    handler.aclose = _close
    return handler


def add_routes(app, pool: asyncpg.Pool, http_getter=None, main_bot=None) -> None:
    handler = make_webhook_route(pool, http_getter, main_bot)
    app.router.add_post(HOOK_PREFIX + "{bot_id}", handler)
    # Своя HTTP-сессия приёмника закрывается вместе с сервером.
    app.on_cleanup.append(handler.aclose)
