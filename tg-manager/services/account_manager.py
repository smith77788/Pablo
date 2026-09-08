"""Telethon user account session management.

Handles session creation, phone/QR login, proxy binding, and
account lifecycle operations for Telegram user accounts.

Usage:
    from services.account_manager import create_client, run_session_health_monitor

    client = await create_client(pool, account_id)
    # ... use client for Telegram operations
"""

from __future__ import annotations
import asyncio
import hashlib
import importlib
import inspect
import threading
from datetime import datetime, timezone, timedelta
import logging
import random
import re
import time
from services.error_codes import ErrorCode, AppError
from services.error_reporting import report_error, get_user_error_message
from typing import Any, Optional

import asyncpg  # noqa: F401 — используется в аннотациях "asyncpg.Pool"
from config import TG_API_ID, TG_API_HASH, TG_PROXY, CF_RELAY_URL
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Процессная политика прокси по умолчанию (переопределяется per-owner через
# device['proxy_policy']). 'allow_direct' сохраняет прежнее лояльное поведение;
# оператор может выставить env PROXY_POLICY=strict для «только через прокси».
import os as _os
from services.proxy_policy import normalize_policy as _normalize_policy, DEFAULT_POLICY as _DEFAULT_POL
_DEFAULT_PROXY_POLICY = _normalize_policy(_os.getenv("PROXY_POLICY", _DEFAULT_POL))

# Per-owner политика прокси (process-local кэш; НЕ переживает рестарт и не шарится
# между воркерами — праймится на входе в операцию из БД). Нужен потому, что
# _resolve_client_proxy синхронна и не может сходить в БД: массовый путь кладёт
# owner_id в словарь аккаунта, а политику берём отсюда.
_OWNER_PROXY_POLICY: dict[int, str] = {}


def set_owner_proxy_policy(owner_id: int | None, policy: str | None) -> None:
    """Обновить кэш политики прокси владельца (зовётся при старте операции и при
    сохранении настроек). None owner_id игнорируется."""
    if owner_id is None:
        return
    _OWNER_PROXY_POLICY[int(owner_id)] = _normalize_policy(policy)

# ── get_me() caching to reduce API calls ────────────────────────────────────
_GET_ME_CACHE: dict[int, tuple[Any, float]] = {}
_GET_ME_TTL = 300  # 5 minutes cache


def _get_cached_me(session_id: int, me: Any) -> Optional[Any]:
    """Get cached get_me() result if still valid."""
    import time as _tm
    now = _tm.monotonic()
    if session_id in _GET_ME_CACHE:
        cached_me, cached_at = _GET_ME_CACHE[session_id]
        if now - cached_at < _GET_ME_TTL:
            return cached_me
    _GET_ME_CACHE[session_id] = (me, now)
    return None


def _invalidate_me_cache(session_id: int) -> None:
    """Invalidate get_me() cache after logout or significant changes."""
    _GET_ME_CACHE.pop(session_id, None)


async def resolve_self_user_id(session_string: str, _acc: dict | None = None) -> Optional[int]:
    """Вернуть Telegram user_id аккаунта по его сессии (me.id), либо None.

    Нужно, когда `tg_accounts.tg_user_id` не заполнен (частый случай при импорте
    сессий): без него автовыдача админки инвайтеру молча пропускалась. Только
    чтение, fail-soft — любая ошибка возвращает None, вызывающий решает дальше.
    """
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        uid = getattr(me, "id", None) if me else None
        return int(uid) if uid else None
    except Exception as exc:
        log.debug("resolve_self_user_id failed: %s", exc)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "resolve_self_user_id: disconnect")


def _parse_proxy(proxy_url: str):
    """Parse socks5://user:pass@host:port → (socks.SOCKS5, host, port, True, user, pass).
    Returns None if proxy_url is empty.
    """
    if not proxy_url:
        return None
    try:
        import socks
        from services.token_vault import decrypt_token

        # proxy_url приходит зашифрованным из user_proxies; passthrough для legacy
        url = decrypt_token(proxy_url).strip()
        if "://" in url:
            url = url.split("://", 1)[1]
        user, password = None, None
        if "@" in url:
            creds, hostpart = url.rsplit("@", 1)
            if ":" in creds:
                user, password = creds.split(":", 1)
            else:
                user = creds
        else:
            hostpart = url
        host, port = hostpart.rsplit(":", 1)
        return (socks.SOCKS5, host, int(port), True, user, password)
    except Exception as e:
        report_error(
            e,
            extra={"proxy_url": proxy_url[:20] + "...", "context": "proxy_parse"},
        )
        log.warning(
            "Failed to parse TG_PROXY: %s — running without proxy", e
        )
        return None


# In-memory pending clients (phone -> client) during login flow
_pending: dict[str, object] = {}

# Device fingerprints for pending phone logins (phone -> device dict)
_pending_device: dict[str, dict] = {}

# QR login sessions: user_id -> (client, qr_login_object, device dict)
_pending_qr: dict[int, tuple] = {}

# Таймаут подключения в секундах
_CONNECT_TIMEOUT = 30
# Бэк-офф авто-ретраев при AUTH_KEY_DUPLICATED (сессия видится с двух IP): конфликт
# временный (лишний коннект логина/фонового цикла ещё закрывается), поэтому пауза +
# переподключение снимают его сами, без ручного «повторите инвайт». Число элементов
# = число ретраев (итоговых попыток на 1 больше). Суммарно ~29с — специально больше
# таймаута фоновой проверки (≤30с): если монитор УЖЕ был подключён к сессии на
# момент захвата операцией (проверка стартовала до in_operation-гейта), операция
# переждёт её завершение и подключится, а не упадёт.
# С процессным мьютексом сессии (ниже) НАШ двойной коннект исключён, поэтому
# оставшийся AUTH_KEY_DUPLICATED — почти всегда внешний/необратимый (сессия
# используется вне системы или ключ уже отозван). Долгий бэк-офф лишь растягивал
# заведомо провальную операцию (в жалобе — op #132 на ~10 мин из-за 29с × N).
# Оставляем ОДИН короткий ретрай на случай настоящего мгновенного пересечения.
_AUTH_DUP_BACKOFF = (5.0,)

# ── Процессный мьютекс на СЕССИЮ (защита от AUTH_KEY_DUPLICATED) ──────────────
# Корень «навсегда мёртвых» аккаунтов: одна и та же сессия коннектится из ДВУХ
# мест одновременно (операция + фоновый цикл/веб-валидация/другая операция) —
# Telegram видит ключ с двух IP и ОТЗЫВАЕТ его безвозвратно. Арбитр операций
# (op_worker.try_claim_account) закрывает лишь пересечение операция↔операция;
# фоновые прогрев/мониторы/валидация импорта коннектятся мимо него. Этот мьютекс
# — единый порог для ЛЮБОГО подключения: одну сессию в один момент держит ровно
# один коннект. Ключ — по самой сессии (то, что видит Telegram), поэтому работает
# независимо от подсистемы и от наличия account_id. Потокобезопасен (threading.Lock),
# т.к. подключения идут из разных event-loop (бот и aiohttp-веб).
_session_inuse_lock = threading.Lock()
_session_inuse: dict[str, float] = {}   # session_key -> момент захвата (для авто-сброса)
# Если release потерялся (краш таски/утечка) — авто-освобождение, чтобы сессия не
# залипла навсегда. Больше самого долгого удержания коннекта на операцию.
_SESSION_INUSE_STALE_S = 300.0
# Сколько операция ждёт освобождения сессии другим коннектом, прежде чем сдаться
# (транзиентный скип, НЕ смерть). Диагностика/валидация ждут 0 — падают сразу.
_SESSION_ACQUIRE_WAIT_S = 25.0


class SessionBusyError(ConnectionError):
    """Сессия уже держится другим подключением — временно недоступна.

    Наследник ConnectionError, чтобы вызывающий код классифицировал её как
    сетевую/транспортную проблему (кулдаун + повтор), а НЕ как смерть аккаунта:
    занятость проходит, как только другой коннект освободит сессию.
    """


def _session_key(session_string: str, device: dict | None) -> str:
    """Стабильный ключ подключения по САМОЙ сессии (её и видит Telegram).

    Хеш session_string сериализует одну и ту же сессию во всех подсистемах.
    Ключ считается по РАСШИФРОВАННОЙ строке (decrypt_token — passthrough для
    plaintext), чтобы шифротекст из БД и уже расшифрованная строка давали ОДИН
    ключ: иначе connect_client (шифр) и raw-коннектор в _make_client (plaintext)
    получили бы РАЗНЫЕ ключи и не исключали бы друг друга. Без сессии (логин по
    номеру) сериализовать нечего — пустой ключ = без мьютекса.
    """
    if session_string:
        try:
            from services.token_vault import decrypt_token
            session_string = decrypt_token(session_string) or session_string
        except Exception:
            pass
        return "s:" + hashlib.sha256(session_string.encode("utf-8", "ignore")).hexdigest()[:32]
    d = device or {}
    if d.get("id"):
        return "a:" + str(d["id"])
    return ""


def _wrap_client_session_mutex(client, session_key: str):
    """Обернуть connect/disconnect клиента процессным мьютексом сессии.

    Для RAW-коннекторов (десятки функций делают _make_client(...).connect() мимо
    connect_client): гарантирует, что одну сессию в момент держит ровно один живой
    коннект — иначе фоновый прогрев/монитор/консоль и операция коннектят её с двух
    IP → AUTH_KEY_DUPLICATED навсегда. connect_client управляет мьютексом сам и
    сюда не попадает (_mutex_managed=True).

    connect: захватываем мьютекс (budget 0 — не ждём); занято → SessionBusyError
    (наследник ConnectionError: raw-коннектор трактует как сетевой сбой и
    отступает, а не создаёт второй коннект). disconnect: освобождаем.
    """
    if client is None or not session_key:
        return client
    _orig_connect = getattr(client, "connect", None)
    _orig_disconnect = getattr(client, "disconnect", None)
    if _orig_connect is None:
        return client
    _st = {"held": False}

    async def _connect(*a, **k):
        if not _st["held"]:
            if not _try_acquire_session(session_key):
                raise SessionBusyError(
                    "сессия занята другим подключением — повтор позже")
            _st["held"] = True
        try:
            r = _orig_connect(*a, **k)
            if inspect.isawaitable(r):
                r = await r
            return r
        except BaseException:
            if _st["held"]:
                _st["held"] = False
                _release_session(session_key)
            raise

    async def _disconnect(*a, **k):
        try:
            if _orig_disconnect is not None:
                r = _orig_disconnect(*a, **k)
                if inspect.isawaitable(r):
                    r = await r
                return r
        finally:
            if _st["held"]:
                _st["held"] = False
                _release_session(session_key)

    try:
        client.connect = _connect  # type: ignore[assignment]
        client.disconnect = _disconnect  # type: ignore[assignment]
    except Exception:
        pass
    return client


def _try_acquire_session(key: str) -> bool:
    if not key:
        return True
    now = time.time()
    with _session_inuse_lock:
        held = _session_inuse.get(key)
        if held is not None and (now - held) < _SESSION_INUSE_STALE_S:
            return False
        _session_inuse[key] = now
        return True


def _release_session(key: str) -> None:
    if not key:
        return
    with _session_inuse_lock:
        _session_inuse.pop(key, None)


def _bind_session_release(client, key: str) -> None:
    """Освободить сессию РОВНО когда возвращённый клиент отключится.

    Оборачиваем disconnect() клиента: релиз наступает по фактическому разрыву
    (span connect→use→disconnect держит мьютекс). Если клиент без disconnect —
    релизим сразу (нечего дожидаться); если caller не отключится — авто-сброс по
    _SESSION_INUSE_STALE_S подстрахует.
    """
    if not key or client is None:
        _release_session(key)
        return
    _orig = getattr(client, "disconnect", None)
    _state = {"released": False}

    def _do_release():
        if not _state["released"]:
            _state["released"] = True
            _release_session(key)

    if _orig is None:
        _do_release()
        return

    async def _wrapped(*a, **k):
        try:
            r = _orig(*a, **k)
            if inspect.isawaitable(r):
                r = await r
            return r
        finally:
            _do_release()

    try:
        client.disconnect = _wrapped  # type: ignore[assignment]
    except Exception:
        _do_release()
# Прогрев кэша участников для резолва инвайтера при автовыдаче админки. Недавно
# вступившие идут первыми в ChannelParticipantsRecent, поэтому потолок умеренный —
# он ограничивает и время, и нагрузку на больших каналах (fail-open при промахе).
_PROMOTE_WARM_LIMIT = 3000
# Таймаут на отдельные Telethon операции (get_entity, send_message и т.д.)
_OP_TIMEOUT = 45

# ── Уникальный IPv6 на аккаунт БЕЗ прокси ─────────────────────────────────────
# Реальная (в отличие от CF-релея с общим edge-IP) изоляция: если хосту
# маршрутизирована IPv6-подсеть (/64, /48 — миллиарды адресов), привязываем
# ИСХОДЯЩИЙ сокет каждого аккаунта к СВОЕМУ адресу из неё (local_addr + use_ipv6).
# Telegram видит уникальный IP на аккаунт без покупки прокси и без CF.
#
# Требование к хосту (иначе bind упадёт): подсеть должна быть routed на сервер и
# включён AnyIP, напр.:  ip -6 route add local <subnet> dev lo
# На Railway routed /64 обычно нет — нужен VPS/dedic с IPv6-блоком. Пусто = выкл
# (нулевое изменение поведения).
_IPV6_SUBNET = _os.getenv("IPV6_SUBNET", "").strip()

# Бесплатный пул публичных SOCKS5 для аккаунтов БЕЗ прокси. По умолчанию ВЫКЛ:
# публичные прокси нестабильны и общие — сессия логинилась через один exit-IP, а
# первая операция уходила через другой (прокси умер между циклами) → скачок IP →
# AUTH_KEY_DUPLICATED / «не ответил». Правильное поведение для безпроксёвого
# аккаунта — стабильный ПРЯМОЙ выход с реального host-IP (одинаковый на логине и в
# операциях). Бесплатный публичный пул УДАЛЁН (блокировал работу): прокси задаёт
# пользователь, иначе прямой host-IP.

# Пер-владелец IPv6-подсеть, заданная в приложении (перекрывает env). Кэш в памяти
# (обновляется при сохранении настроек), + читается из БД в get_account_for_telethon.
_OWNER_IPV6_SUBNET: dict[int, str] = {}


def set_owner_ipv6_subnet(owner_id: int | None, subnet: str | None) -> None:
    """Обновить in-memory IPv6-подсеть владельца (вызывается при сохранении в БД)."""
    if owner_id is None:
        return
    s = (subnet or "").strip()
    if s:
        _OWNER_IPV6_SUBNET[int(owner_id)] = s
    else:
        _OWNER_IPV6_SUBNET.pop(int(owner_id), None)


# ── ТРАНСПОРТ АККАУНТА: страховка от неполной выборки вызывающего ────────────
# Транспорт выбирается ПО ПОЛЯМ словаря, который передали в _make_client. В коде
# больше сотни собственных SELECT-ов к tg_accounts, и подавляющее большинство не
# берёт cf_relay_url, а часть — и proxy_id. Такой словарь молча уводит аккаунт
# НАПРЯМУЮ с host-IP, тогда как канонический путь (db.telethon_accounts_query)
# ведёт тот же аккаунт через релей или назначенный прокси. Одна сессия с двух
# адресов — AUTH_KEY_DUPLICATED, Telegram отзывает ключ, а снаружи это выглядит
# как «аккаунты сами отваливаются».
#
# Переписать сотню запросов разом нельзя без риска, поэтому здесь — карта
# acc_id → транспортные поля, которой _make_client ДОБИРАЕТ то, чего в словаре
# НЕТ. Именно добирает: явно переданное значение (в том числе пустое — это
# законное «релей не назначен») всегда сильнее кэша.
_ACC_TRANSPORT: dict[int, dict[str, Any]] = {}

# Ключи, которые умеет добирать кэш. Ровно те, от которых зависит выбор выхода.
_TRANSPORT_KEYS = ("cf_relay_url", "proxy_id", "proxy_url")
# owner_id в записи не добирается — он нужен приймингу, чтобы знать, чьи записи
# он вправе считать устаревшими.
_TRANSPORT_META = ("owner_id",)


def set_account_transport(account_id: int | None, **fields: Any) -> None:
    """Запомнить транспортные поля аккаунта (вызывать при их изменении в БД).

    Кэш обязан меняться ВМЕСТЕ с базой: устаревшая запись уведёт аккаунт через
    снятый релей — та же авария, только с другой стороны.
    """
    if account_id is None:
        return
    entry = _ACC_TRANSPORT.setdefault(int(account_id), {})
    for k, v in fields.items():
        if k in _TRANSPORT_KEYS:
            entry[k] = v
        elif k in _TRANSPORT_META and v is not None:
            entry[k] = int(v)


def forget_account_transport(account_id: int | None) -> None:
    """Убрать аккаунт из карты — при удалении аккаунта."""
    if account_id is not None:
        _ACC_TRANSPORT.pop(int(account_id), None)


async def prime_account_transport(pool: Any, owner_id: int | None = None) -> int:
    """Перечитать карту транспорта из БД. Возвращает число аккаунтов в карте.

    Берём только аккаунты с НЕдефолтным выходом (есть релей или назначен
    прокси): остальным добирать нечего — они и так идут напрямую.

    Прайминг АВТОРИТЕТЕН для тех владельцев, которых перечитали: записи, не
    подтверждённые свежей выборкой, удаляются. Иначе аккаунт, у которого релей
    сняли, навсегда остался бы в памяти со старым релеем — и мы бы своими
    руками воспроизвели ту же аварию, только с другой стороны. Поэтому запись
    хранит owner_id: без него «чьи записи чистить» не ответить.

    Ошибку НЕ проглатываем в «карта пуста»: пустая карта неотличима от «ни у
    кого нет релея» и снова уводит аккаунты напрямую. Исключение уходит наверх,
    прежняя карта остаётся.
    """
    where = "WHERE (a.cf_relay_url IS NOT NULL AND a.cf_relay_url <> '') OR a.proxy_id IS NOT NULL"
    args: list[Any] = []
    if owner_id is not None:
        where = "WHERE a.owner_id = $1 AND ((a.cf_relay_url IS NOT NULL " \
                "AND a.cf_relay_url <> '') OR a.proxy_id IS NOT NULL)"
        args = [int(owner_id)]
    rows = await pool.fetch(
        "SELECT a.id, a.owner_id, a.cf_relay_url, a.proxy_id, p.proxy_url "
        "FROM tg_accounts a "
        "LEFT JOIN user_proxies p ON p.id = a.proxy_id AND p.is_active = TRUE "
        + where, *args)

    fresh = {int(r["id"]) for r in rows}
    # Снимаем устаревшие записи — только по перечитанной области.
    for acc_id in [k for k, v in _ACC_TRANSPORT.items()
                   if k not in fresh
                   and (owner_id is None or v.get("owner_id") == int(owner_id))]:
        _ACC_TRANSPORT.pop(acc_id, None)

    for r in rows:
        set_account_transport(
            r["id"], owner_id=r["owner_id"], cf_relay_url=r["cf_relay_url"],
            proxy_id=r["proxy_id"], proxy_url=r["proxy_url"])
    return len(_ACC_TRANSPORT)


# Как часто веб-процесс перечитывает карту. Пять минут — компромисс: назначение
# релея редкое событие, а запрос берёт только аккаунты с недефолтным выходом.
_TRANSPORT_REFRESH_SEC = 300


async def run_transport_refresh_loop(pool: Any, interval: int = _TRANSPORT_REFRESH_SEC) -> None:
    """Держать карту транспорта свежей ВО ВСЕХ ролях процесса.

    Прайминг на старте операции закрывает только массовые пути. Но в роли
    INFRAGRAM_ROLE=web фоновые циклы не запускаются вовсе, а мини-апп и бот
    ходят к аккаунтам по своим выборкам — и без карты уводят аккаунт с релеем
    напрямую. Поэтому цикл вешается на негейтованный запуск.

    Своя ошибка цикл не роняет: карта — страховка, и её недоступность не должна
    останавливать процесс. Но и молчать нельзя — иначе рассинхрон транспорта
    снова станет невидимым.
    """
    while True:
        try:
            n = await prime_account_transport(pool)
            log.debug("карта транспорта обновлена: %d аккаунтов", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc_swallow(log, "обновление карты транспорта аккаунтов")
        await asyncio.sleep(interval)


def _fill_transport_fields(device: dict[str, Any]) -> list[str]:
    """Добрать в словарь отсутствующие транспортные поля. Вернуть добранные.

    Только ОТСУТСТВУЮЩИЕ ключи: пустое значение в словаре — это ответ выборки
    («релея нет»), и подменять его кэшем нельзя.
    """
    acc_id = device.get("id")
    if acc_id is None:
        return []
    try:
        cached = _ACC_TRANSPORT.get(int(acc_id))
    except (TypeError, ValueError):
        return []
    if not cached:
        return []
    filled = []
    for k in _TRANSPORT_KEYS:
        if k not in device and k in cached:
            device[k] = cached[k]
            filled.append(k)
    return filled


def _account_ipv6(account_id: int, subnet_cidr: str) -> str | None:
    """Детерминированно вернуть УНИКАЛЬНЫЙ IPv6 аккаунта из подсети (или None).

    account_id → фиксированный адрес: один аккаунт всегда с одного IP (стабильный
    exit, без AUTH_KEY-рассинхрона), разные аккаунты — с разных. Не выдаёт
    network/anycast-нулевой адрес."""
    if not subnet_cidr or not account_id:
        return None
    try:
        import ipaddress
        net = ipaddress.ip_network(subnet_cidr, strict=False)
        if net.version != 6 or net.num_addresses <= 2:
            return None
        offset = (int(account_id) % (net.num_addresses - 2)) + 1
        return str(net[offset])
    except Exception as e:
        log.warning("_account_ipv6(%s, %s) failed: %s", account_id, subnet_cidr, e)
        return None
_TELEGRAM_MAX_MUTE_UNTIL = datetime.fromtimestamp(2_147_483_647, tz=timezone.utc)

_TG_INVITE_RE = re.compile(
    r"^(?:https?://)?(?:t|telegram)\.(?:me|dog)/(?:joinchat/|\+)([\w-]+)",
    re.IGNORECASE,
)
_TG_JOIN_URI_RE = re.compile(r"^tg://join\?invite=([\w-]+)", re.IGNORECASE)

# ── Free proxy pool: УДАЛЁН ────────────────────────────────────────────────────
# Бесплатный публичный пул SOCKS5 полностью удалён из транспорта: публичные прокси
# нестабильны, умирают между логином и операцией → сессия видится Telegram с разных
# IP → AUTH_KEY_DUPLICATED, и это БЛОКИРОВАЛО работу флота. Политика транспорта:
# прокси задаёт ПОЛЬЗОВАТЕЛЬ (при подключении аккаунтов или позже); если прокси нет —
# стабильный ПРЯМОЙ host-IP; CF-релей/IPv6 — только по явному выбору. Функция-заглушка
# сохранена, чтобы proxy_scraper (если ещё вызывает) не падал — пул больше не влияет
# на транспорт.
def set_pool_proxy_cache(urls: list[str]) -> None:  # noqa: D401 - deprecated no-op
    """DEPRECATED: free-pool удалён. No-op — публичный пул больше не используется."""
    return None


def _record_proxy_fail(acc: dict | None, action_type: str) -> None:
    """Record proxy failure in infra_memory when connect/op fails with a network error.
    Non-blocking — no pool needed. Degrades proxy score so future ops avoid it."""
    if not acc:
        return
    proxy_url = acc.get("proxy_url") or ""
    if not proxy_url:
        return
    try:
        from services import infra_memory

        infra_memory.record_proxy_op(proxy_url, action_type, success=False)
    except Exception as e:
        log.warning("_record_proxy_fail: %s", e)


def _record_proxy_ok(acc: dict | None, action_type: str, latency_ms: int = 0) -> None:
    """Записать УСПЕХ прокси (успешный коннект) в infra_memory — симметрично
    _record_proxy_fail. Без этого success_rate прокси считался только по провалам,
    и proxy_selector штрафовал рабочие прокси. Non-blocking."""
    if not acc:
        return
    proxy_url = acc.get("proxy_url") or ""
    if not proxy_url:
        return
    try:
        from services import infra_memory

        infra_memory.record_proxy_op(
            proxy_url, action_type, success=True, latency_ms=latency_ms
        )
    except Exception:
        pass


async def _connect_and_track(client, acc: dict | None, action_type: str) -> None:
    """Подключить клиента и записать УСПЕХ прокси при удачном коннекте.

    Провалы коннекта ловятся except-блоками вызывающего кода (_record_proxy_fail),
    поэтому здесь фиксируем только успех — чтобы infra_memory видел оба сигнала
    и success_rate прокси отражал реальность, а не только сбои. При ошибке коннект
    пробрасывается наверх без изменений (успех не пишется)."""
    t0 = time.time()
    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
    _record_proxy_ok(acc, action_type, int((time.time() - t0) * 1000))


# ── Proxy Intelligence (интеллект прокси) ───────────────────────────────────
# Автотест, автопереключение, статистика, рекомендации.

_proxy_stats: dict[str, dict] = {}  # proxy_url → {success, fail, avg_latency, last_check}


async def test_proxy(proxy_url: str, timeout: float = 5.0) -> dict:
    """Test proxy connectivity and latency. Returns {ok, latency_ms, error}."""
    if not proxy_url:
        return {"ok": False, "error": "No proxy URL"}
    
    try:
        import socks
        import socket
        
        # Parse proxy URL
        parsed = _parse_proxy(proxy_url)
        if not parsed:
            return {"ok": False, "error": "Invalid proxy URL format"}
        
        # Create SOCKS proxy socket
        s = socks.socksocket()
        s.set_proxy(parsed[0], parsed[1], parsed[2], rdns=True, 
                    username=parsed[4], password=parsed[5])
        s.settimeout(timeout)
        
        # Test connection to Telegram DC
        t0 = time.time()
        s.connect(("149.154.167.50", 443))  # Telegram DC1
        latency_ms = int((time.time() - t0) * 1000)
        s.close()
        
        # Record success
        _record_proxy_stat(proxy_url, True, latency_ms)
        
        return {"ok": True, "latency_ms": latency_ms}
    except Exception as e:
        _record_proxy_stat(proxy_url, False, 0)
        return {"ok": False, "error": str(e)[:100]}


def _record_proxy_stat(proxy_url: str, success: bool, latency_ms: int) -> None:
    """Record proxy test result for statistics."""
    stats = _proxy_stats.setdefault(proxy_url, {
        "success": 0, "fail": 0, "latencies": [], "last_check": 0
    })
    if success:
        stats["success"] += 1
        stats["latencies"].append(latency_ms)
        # Keep last 20 latencies
        if len(stats["latencies"]) > 20:
            stats["latencies"] = stats["latencies"][-20:]
    else:
        stats["fail"] += 1
    stats["last_check"] = time.time()


def get_proxy_stats(proxy_url: str) -> dict:
    """Get proxy statistics: success_rate, avg_latency, total_checks.

    Приоритет — реальные операционные данные из infra_memory (успех/провал
    коннекта в реальных операциях). test_proxy-статистика (_proxy_stats)
    используется как fallback, если операций по прокси ещё не было."""
    try:
        from services import infra_memory

        summary = infra_memory.get_proxy_summary(proxy_url)
    except Exception:
        summary = None
    if summary:
        return {
            "success_rate": summary["success_rate"],
            "avg_latency_ms": summary["avg_latency_ms"],
            "total_checks": summary["total"],
            "last_check": summary["last_check"],
        }

    stats = _proxy_stats.get(proxy_url, {})
    total = stats.get("success", 0) + stats.get("fail", 0)
    if total == 0:
        return {"success_rate": 0, "avg_latency_ms": 0, "total_checks": 0}

    latencies = stats.get("latencies", [])
    avg_latency = sum(latencies) // len(latencies) if latencies else 0

    return {
        "success_rate": round(stats["success"] / total * 100, 1),
        "avg_latency_ms": avg_latency,
        "total_checks": total,
        "last_check": stats.get("last_check", 0),
    }


async def auto_select_proxy(account_id: int, pool: "asyncpg.Pool") -> str | None:
    """Auto-select best proxy for account based on statistics and latency."""
    # Get account's current proxy
    acc = await pool.fetchrow(
        "SELECT proxy_id FROM tg_accounts WHERE id=$1", account_id
    )
    if not acc or not acc["proxy_id"]:
        return None
    
    proxy = await pool.fetchrow(
        "SELECT id, proxy_url, is_active FROM user_proxies WHERE id=$1",
        acc["proxy_id"],
    )
    if not proxy or not proxy["is_active"]:
        return None
    
    # Test current proxy
    result = await test_proxy(proxy["proxy_url"])
    if result["ok"]:
        return proxy["proxy_url"]
    
    # Current proxy failed — try to find a better one
    alternatives = await pool.fetch(
        "SELECT proxy_url FROM user_proxies WHERE is_active=TRUE AND id != $1",
        acc["proxy_id"],
    )
    
    for alt in alternatives:
        alt_result = await test_proxy(alt["proxy_url"])
        if alt_result["ok"]:
            log.info("proxy_intelligence: acc=%d switching proxy %d→%s (latency=%dms)",
                     account_id, acc["proxy_id"], alt["proxy_url"][:20], alt_result["latency_ms"])
            return alt["proxy_url"]
    
    return None  # No working proxy found


_TG_PUBLIC_RE = re.compile(
    r"^(?:https?://)?(?:t|telegram)\.(?:me|dog)/(?:s/)?([A-Za-z0-9_]{5,32})(?:[/?#].*)?$",
    re.IGNORECASE,
)


_DEAD_SESSION_MARKERS = (
    "auth_key_unregistered",
    "key is not registered",
    "registered in the system",
    "session_revoked",
    "session_expired",
    "user_deactivated",
)


def is_dead_session_error(error_text: str | None) -> bool:
    """True if an operation error indicates the account session is dead or the
    account is banned/deleted — callers should deactivate the account in DB."""
    if not error_text:
        return False
    low = str(error_text).lower()
    return any(m in low for m in _DEAD_SESSION_MARKERS)


def normalize_telegram_join_ref(value: str) -> tuple[str, str]:
    """Normalize Telegram join targets to official invite/public URL shapes."""
    raw = value.strip()
    if not raw:
        return ("public", "")

    raw_no_fragment = raw.split("#", 1)[0].strip()
    invite_match = _TG_INVITE_RE.match(raw_no_fragment) or _TG_JOIN_URI_RE.match(
        raw_no_fragment
    )
    if invite_match:
        return ("invite", invite_match.group(1))
    if raw_no_fragment.startswith("+") and len(raw_no_fragment) > 1:
        return ("invite", raw_no_fragment[1:].split("?", 1)[0])

    public_match = _TG_PUBLIC_RE.match(raw_no_fragment)
    if public_match:
        return ("public", public_match.group(1))
    if raw_no_fragment.startswith("@"):
        return ("public", raw_no_fragment[1:].split("?", 1)[0])
    return ("public", raw_no_fragment.split("?", 1)[0])


def format_telegram_join_ref_display(value: str) -> str:
    """Return a user-facing Telegram target without mixing invite links and @names."""
    ref_kind, ref_value = normalize_telegram_join_ref(value)
    if not ref_value:
        return ""
    if ref_kind == "invite":
        return f"https://t.me/+{ref_value}"
    return f"@{ref_value}"


def _select_report_option_for_reason(options: list, reason: str) -> bytes | None:
    """Pick the best report option Telegram offered for the requested reason."""
    hints = {
        "spam": ("spam", "unwanted", "advertising", "unsolicited", "спам", "реклам"),
        "violence": ("violence", "violent", "harm", "abuse", "насил", "жест"),
        "pornography": ("porn", "sexual", "adult", "explicit", "nudity", "порно"),
        "childabuse": ("child", "minor", "children", "underage", "csam", "дет"),
        "copyright": ("copyright", "dmca", "intellectual", "автор"),
        "drugs": ("drug", "substance", "narcotic", "нарко"),
        "personal": ("personal", "private", "privacy", "личн", "данн"),
        "fake": ("fake", "scam", "fraud", "impersonat", "мошен", "фейк"),
        "other": ("other", "else", "другое", "иное"),
        "weapons": ("weapon", "arms", "firearm", "explosive", "оружи"),
        "terrorism": ("terror", "extremi", "incit", "террор", "экстрем"),
        "fraud": ("fraud", "scam", "financial", "мошен", "финанс"),
        "escort": ("escort", "prostit", "sexual", "услуг", "сексуальн"),
        "geo": ("geo", "irrelevant", "geography", "геогр"),
    }.get(reason, ())
    for opt in options:
        text = (getattr(opt, "text", "") or "").lower()
        if any(hint in text for hint in hints):
            value = getattr(opt, "option", None)
            if value is not None:
                return value
    if options:
        return getattr(options[0], "option", None)
    return None


async def _submit_message_report(
    client: Any,
    request_cls: Any,
    peer_obj: Any,
    msg_ids: list[int],
    comment: str,
    reason: str,
    stage: str,
) -> bool:
    """Submit a message report using Telegram's option-based flow."""
    from telethon.tl.types import (
        ReportResultAddComment,
        ReportResultChooseOption,
        ReportResultReported,
    )

    async def _traverse(option: bytes, depth: int) -> bool:
        if depth > 5:
            return False
        try:
            result = await asyncio.wait_for(
                client(
                    request_cls(
                        peer=peer_obj,
                        id=msg_ids,
                        option=option,
                        message=comment if depth > 0 else "",
                    )
                ),
                timeout=_OP_TIMEOUT,
            )
        except Exception as exc:
            log.warning("%s: %s", stage, exc)
            return False
        if isinstance(result, ReportResultReported):
            return True
        if isinstance(result, ReportResultAddComment):
            try:
                final = await asyncio.wait_for(
                    client(
                        request_cls(
                            peer=peer_obj,
                            id=msg_ids,
                            option=result.option,
                            message=comment,
                        )
                    ),
                    timeout=_OP_TIMEOUT,
                )
                return isinstance(final, ReportResultReported)
            except Exception as exc:
                log.warning("%s: %s", stage, exc)
                return False
        if isinstance(result, ReportResultChooseOption):
            options = result.options or []
            preferred = _select_report_option_for_reason(options, reason)
            ordered: list[bytes] = []
            if preferred is not None:
                ordered.append(preferred)
            for opt in options:
                value = getattr(opt, "option", None)
                if value is not None and value != preferred:
                    ordered.append(value)
            for value in ordered:
                await asyncio.sleep(random.uniform(0.25, 0.75))
                if await _traverse(value, depth + 1):
                    return True
        return False

    return await _traverse(b"", 0)


# ── Отпечатки клиента: СТАРЕЮЩИЙ АКТИВ, требует регулярной актуализации ───────
#
# Anti-detection: пул отпечатков нельзя «сделать один раз». Telegram-клиент
# АВТООБНОВЛЯЕТСЯ, поэтому аккаунт, заявляющий прошлогоднюю версию приложения,
# выпадает из реальной популяции — а весь наш пул на одной устаревшей версии
# превращается в КОГОРТНУЮ СИГНАТУРУ (все наши аккаунты похожи друг на друга и
# не похожи на живых людей). Это дороже обычного бага.
#
# Важная асимметрия:
#   * версия ПРИЛОЖЕНИЯ стареть НЕ должна — клиент обновляется сам у всех;
#   * модель УСТРОЙСТВА стареть МОЖЕТ и должна — люди держат телефон 3-4 года,
#     пул из одних новинок так же неправдоподобен, как пул из одного старья.
# Поэтому список устройств — широкий (от флагманов до бюджеток разных лет), а
# версии приложения держим в узком «свежем» окне последних месяцев.
#
# ОБНОВЛЕНИЕ БЕЗ ДЕПЛОЯ: оба пула переопределяются через env (см. ниже) — при
# выходе новой версии Telegram достаточно поменять переменную окружения.
# ПРИНУДИТЕЛЬНАЯ РЕВИЗИЯ: дату ниже сторожит tests/test_fingerprint_freshness.py —
# он падает, когда пул не пересматривали дольше FINGERPRINT_MAX_AGE_DAYS.
# При ревизии: сверить актуальную версию Telegram Android и поднять дату.
FINGERPRINT_REVIEWED = "2026-07-25"  # ISO-дата последней сверки с реальностью
FINGERPRINT_MAX_AGE_DAYS = 120       # дольше — пул считается протухшим (гейт красный)

# Модели проверены как реально существующие: выдуманный код модели сам по себе
# палево (такой строки нет в живой популяции).
_DEFAULT_ANDROID_DEVICES: list[tuple[str, str]] = [
    # актуальные флагманы (получили свежую ОС)
    ("Samsung SM-S938B", "Android 17"),
    ("Samsung SM-S931B", "Android 17"),
    ("Google Pixel 10 Pro", "Android 17"),
    ("Google Pixel 9 Pro", "Android 17"),
    ("Samsung SM-S928B", "Android 16"),
    ("Google Pixel 8 Pro", "Android 16"),
    ("OnePlus 12", "Android 16"),
    ("Xiaomi 14 Pro", "Android 16"),
    # массовый сегмент прошлых лет (обновились частично)
    ("Samsung SM-S918B", "Android 15"),
    ("Samsung SM-S911B", "Android 15"),
    ("Xiaomi 13T Pro", "Android 15"),
    ("Google Pixel 7a", "Android 15"),
    ("OnePlus 11", "Android 15"),
    ("Samsung SM-A546B", "Android 15"),
    ("POCO X6 Pro", "Android 15"),
    ("Motorola Edge 50 Pro", "Android 15"),
    # бюджетки и старые аппараты — реалистично отстают
    ("Xiaomi Redmi Note 13 Pro", "Android 14"),
    ("realme GT 5 Pro", "Android 14"),
    ("Samsung SM-A336B", "Android 14"),
    ("Vivo V27 Pro", "Android 14"),
    ("Motorola Moto G84", "Android 14"),
    ("Samsung SM-A135F", "Android 13"),
    ("Xiaomi POCO M5s", "Android 13"),
    ("Nokia G60 5G", "Android 13"),
]
# Узкое окно свежих версий: клиент автообновляется, «хвост» на старых мажорах
# неправдоподобен. Сверено 2026-07: актуальная ветка Telegram Android — 12.9.x.
_DEFAULT_APP_VERSIONS: list[str] = [
    "12.9.1",
    "12.9.0",
    "12.8.2",
    "12.8.0",
    "12.7.3",
    "12.7.1",
    "12.6.2",
    "12.6.0",
    "12.5.1",
]


def _pool_from_env(var: str, fallback):
    """Переопределение пула через env (JSON) — актуализация без деплоя.

    Формат: TG_APP_VERSIONS='["12.9.1","12.9.0"]'
            TG_ANDROID_DEVICES='[["Samsung SM-S938B","Android 17"]]'
    Любая ошибка разбора/пустое значение → дефолт из кода (fail-safe: лучше
    работать на встроенном пуле, чем упасть на старте из-за опечатки в env).
    """
    raw = _os.getenv(var, "").strip()
    if not raw:
        return fallback
    try:
        import json as _json
        parsed = _json.loads(raw)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("ожидался непустой список")
        if isinstance(fallback[0], tuple):
            out = [(str(a), str(b)) for a, b in parsed]
        else:
            out = [str(x) for x in parsed]
        log.info("fingerprint: пул %s переопределён из env (%d значений)", var, len(out))
        return out
    except Exception as e:
        log.warning("fingerprint: не удалось разобрать %s (%s) — беру встроенный пул", var, e)
        return fallback


_ANDROID_DEVICES: list[tuple[str, str]] = _pool_from_env(
    "TG_ANDROID_DEVICES", _DEFAULT_ANDROID_DEVICES)
_APP_VERSIONS: list[str] = _pool_from_env(
    "TG_APP_VERSIONS", _DEFAULT_APP_VERSIONS)

_COUNTRY_LOCALES: dict[str, tuple[str, str]] = {
    "RU": ("ru", "ru-RU"),
    "UA": ("uk", "uk-UA"),
    "BY": ("be", "be-BY"),
    "KZ": ("ru", "ru-KZ"),
    "DE": ("de", "de-DE"),
    "AT": ("de", "de-AT"),
    "CH": ("de", "de-CH"),
    "FR": ("fr", "fr-FR"),
    "BE": ("fr", "fr-BE"),
    "IT": ("it", "it-IT"),
    "ES": ("es", "es-ES"),
    "PL": ("pl", "pl-PL"),
    "TR": ("tr", "tr-TR"),
    "GB": ("en", "en-GB"),
    "IE": ("en", "en-IE"),
    "US": ("en", "en-US"),
    "CA": ("en", "en-CA"),
}


class ProxyIsolationError(ConnectionError):
    """Raised when an account-bound proxy is missing, invalid, or unavailable."""


def _locale_for_country(country_code: str | None) -> tuple[str, str]:
    if not country_code:
        return ("ru", "ru-RU")
    return _COUNTRY_LOCALES.get(country_code.strip().upper(), ("en", "en-US"))


# E.164 calling-code prefixes → ISO 3166-1 alpha-2. Longest prefix first so
# multi-digit codes (e.g. "44" GB) aren't shadowed by a shorter one that isn't
# actually a prefix of it. Covers _COUNTRY_LOCALES plus the SMS-provider
# "popular" country list (auto_registrar.py) so auto-registered numbers get a
# locale/device fingerprint that actually matches the phone's real country
# instead of defaulting to ru-RU regardless of where the number is from.
_CALLING_CODES: list[tuple[str, str]] = sorted(
    [
        ("7", "RU"), ("380", "UA"), ("375", "BY"), ("77", "KZ"),
        ("49", "DE"), ("43", "AT"), ("41", "CH"), ("33", "FR"),
        ("32", "BE"), ("39", "IT"), ("34", "ES"), ("48", "PL"),
        ("90", "TR"), ("44", "GB"), ("353", "IE"), ("1", "US"),
        ("91", "IN"), ("62", "ID"), ("55", "BR"), ("63", "PH"),
    ],
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def country_code_from_phone(phone: str | None) -> str | None:
    """Best-effort ISO country code from an E.164 phone number's calling code."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    for prefix, iso in _CALLING_CODES:
        if digits.startswith(prefix):
            return iso
    return None


def _normalize_device_profile(device: dict | None = None) -> dict[str, Any]:
    payload = dict(device or {})
    lang_code = payload.get("lang_code")
    system_lang_code = payload.get("system_lang_code")
    if not lang_code or not system_lang_code:
        locale_lang, locale_system = _locale_for_country(payload.get("geo_country"))
        payload.setdefault("lang_code", locale_lang)
        payload.setdefault("system_lang_code", locale_system)
    payload.setdefault("device_model", "Samsung SM-S911B")
    payload.setdefault("system_version", "Android 14")
    payload.setdefault("app_version", "11.5.3")
    # Пер-аккаунтное Telegram-приложение: весь флот под одной парой
    # TG_API_ID/HASH — прямой корреляционный признак связности когорты.
    # Пул пуст → пара из config (поведение ровно прежнее).
    if not payload.get("api_id") or not payload.get("api_hash"):
        try:
            from services import tg_apps

            _pair = tg_apps.for_account(
                payload.get("id") or payload.get("acc_id"),
                payload.get("api_id"),
            )
            if _pair:
                payload["api_id"], payload["api_hash"] = _pair[0], _pair[1]
        except Exception:
            log_exc_swallow(log, "tg_apps: не удалось выбрать приложение")
    return payload


async def pick_registration_proxy(
    pool: Any, owner_id: int, country_code: str | None = None
) -> tuple[int, str] | None:
    """Pick a proxy from the owner's pool for a *new* account registration.

    Prefers a proxy geo-matched to `country_code`, falling back to any active
    proxy for the owner. Picks the least-loaded one (fewest accounts already
    on it) so a batch of registrations spreads across the pool instead of
    reusing a single proxy for every number. Returns (proxy_id, proxy_url) or
    None if the owner has no usable proxies — callers fall back to the
    existing global/free-pool behavior in _make_client, same as before.
    """
    rows = await pool.fetch(
        """SELECT p.id, p.proxy_url,
                  (SELECT COUNT(*) FROM tg_accounts a WHERE a.proxy_id = p.id) AS load
           FROM user_proxies p
           WHERE p.owner_id = $1 AND p.is_active = TRUE
             AND (p.is_alive IS NULL OR p.is_alive = TRUE)
           ORDER BY (UPPER(p.geo_country) = UPPER($2)) DESC NULLS LAST, load ASC
           LIMIT 1""",
        owner_id, country_code or "",
    )
    if not rows:
        return None
    row = rows[0]
    return int(row["id"]), str(row["proxy_url"])


def _resolve_client_proxy(device: dict[str, Any], low_risk: bool = False) -> Any:
    """Выбрать прокси для клиента согласно политике изоляции.

    low_risk=True — низкорисковая одиночная операция (напр. чтение контактов):
    её не блокируем из-за отсутствия прокси. Глобальная политика владельца
    берётся из device['proxy_policy'] ('strict'|'allow_direct'), по умолчанию —
    процессная _DEFAULT_PROXY_POLICY.
    """
    # Аккаунт-словарь может пометить соединение низкорисковым (одиночное чтение),
    # чтобы не тащить low_risk через все сигнатуры движков.
    low_risk = low_risk or bool(device.get("_low_risk"))
    acc_proxy_url = str(device.get("proxy_url") or "").strip()
    proxy = _parse_proxy(acc_proxy_url) if acc_proxy_url else None
    # «Привязан к прокси» = НАЗНАЧЕН proxy_id ЛИБО есть URL. Ключевой момент: если
    # proxy_id задан, но прокси неактивен (JOIN вернул proxy_url=NULL), аккаунт всё
    # равно привязан к IP этого прокси — подключать его напрямую НЕЛЬЗЯ (иначе
    # AUTH_KEY_DUPLICATED убьёт сессию). Поэтому опираемся на proxy_id, а не только
    # на наличие распарсенного URL.
    has_assigned_proxy = bool(acc_proxy_url) or bool(device.get("proxy_id"))
    # Назначенный, но битый/неактивный прокси — жёсткий блок В ЛЮБОМ режиме (включая
    # low_risk): сессия привязана к IP прокси, ходить с другого адреса нельзя.
    if has_assigned_proxy and proxy is None:
        raise ProxyIsolationError(
            "Аккаунту назначен прокси, но он недоступен — исправьте прокси (подключение "
            "с другого IP убьёт сессию: AUTH_KEY_DUPLICATED)."
        )
    if proxy is not None:
        return proxy
    # Нет назначенного прокси: отдаём глобальный TG_PROXY (если задан), иначе None.
    # Решение «идти ли НАПРЯМУЮ с IP хоста» принимает _make_client В КОНЦЕ цепочки
    # транспорта (после CF-relay/IPv6/free-pool) — kill-switch strict ставится там,
    # чтобы strict НЕ рубил легитимные не-host транспорты (реле/IPv6/пул).
    return _parse_proxy(TG_PROXY) if TG_PROXY else None


def _effective_proxy_policy(device: dict[str, Any]) -> str:
    """Итоговая политика прокси для аккаунта: явная в словаре → per-owner кэш
    (массовый путь кладёт owner_id) → процессный дефолт (_DEFAULT_PROXY_POLICY)."""
    policy = device.get("proxy_policy")
    if not policy:
        _oid = device.get("owner_id")
        policy = _OWNER_PROXY_POLICY.get(int(_oid)) if _oid is not None else None
    return _normalize_policy(policy or _DEFAULT_PROXY_POLICY)


def device_manufacturers() -> list[str]:
    """Distinct manufacturer names available in the device pool, in a stable
    order — used to populate the "Генератор параметров" picker."""
    seen: list[str] = []
    for model, _ in _ANDROID_DEVICES:
        make = model.split()[0]
        if make not in seen:
            seen.append(make)
    return seen


def app_versions() -> list[str]:
    return list(_APP_VERSIONS)


def generate_device_fingerprint(
    country_code: str | None = None,
    manufacturer: str | None = None,
    app_version: str | None = None,
) -> dict[str, str]:
    """Return a realistic Android device fingerprint with a locale binding.

    manufacturer/app_version let a caller pin the emulated device to a
    specific make (e.g. "Samsung") or Telegram build instead of the fully
    random pick — mirrors the "Генератор параметров" control competitor
    account-farming panels expose (Производитель / Версия приложения).
    """
    pool = _ANDROID_DEVICES
    if manufacturer:
        filtered = [d for d in pool if d[0].split()[0].lower() == manufacturer.strip().lower()]
        pool = filtered or pool
    device_model, system_version = random.choice(pool)
    lang_code, system_lang_code = _locale_for_country(country_code)
    return {
        "device_model": device_model,
        "system_version": system_version,
        "app_version": app_version if app_version in _APP_VERSIONS else random.choice(_APP_VERSIONS),
        "lang_code": lang_code,
        "system_lang_code": system_lang_code,
    }


def _make_client(session_string: str = "", device: dict | None = None, low_risk: bool = False,
                 _no_pool: bool = False, _force_direct: bool = False,
                 _mutex_managed: bool = False):
    """Создать TelegramClient с правильным fingerprint и транспортом.

    _no_pool=True — НЕ брать прокси из free-pool для безпроксёвого аккаунта, а идти
    напрямую (host IP). Используется `connect_client` как fallback, когда пул-прокси
    не отвечает: под allow_direct прямой стабильный IP лучше мёртвого публичного
    прокси, иначе аккаунт без назначенного прокси не подключается вовсе.
    На возвращаемый клиент вешается атрибут `_infragram_transport`
    ('bound'|'ipv6'|'relay'|'pool'|'direct') — по нему `connect_client` решает,
    можно ли фолбэкнуться на прямое подключение.

    Приоритет транспорта (от высшего к низшему):
    1. Аккаунт-bound прокси (proxy_url в device dict) — строгая изоляция,
       используется если у аккаунта назначен конкретный прокси.
    2. Глобальный TG_PROXY (socks5://...) — явно заданный оператором прокси.
    3. Пер-аккаунтный/глобальный CF_RELAY_URL — Cloudflare Worker relay (opt-in).
    4. Уникальный IPv6 аккаунта (IPV6_SUBNET, opt-in) — стабильный per-account IP.
    5. Бесплатный пул публичных SOCKS5 — ТОЛЬКО при USE_FREE_POOL=1 (по умолчанию
       ВЫКЛ: публичные прокси нестабильны → скачок IP → AUTH_KEY_DUPLICATED).
    6. Прямое подключение с реального host-IP (ConnectionTcpObfuscated) — ДЕФОЛТ
       для аккаунта без прокси/релея/IPv6: стабильный один и тот же IP на логине и
       в операциях (host-IP виден Telegram, но сессия не «прыгает» по адресам).

    Всегда используется обфускация (ConnectionTcpObfuscated или CF relay поверх неё),
    никогда ConnectionTcpFull — он легко детектится как Telethon/MTProto.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.network.connection.tcpobfuscated import ConnectionTcpObfuscated
    from services.token_vault import decrypt_token

    # Единственная точка потребления сессии → здесь же расшифровываем.
    # decrypt_token — passthrough для legacy-plaintext, поэтому старые строки
    # продолжают работать без миграции.
    session_string = decrypt_token(session_string or "")

    d = _normalize_device_profile(device)

    # ── НЕПОЛНЫЙ СЛОВАРЬ АККАУНТА — МОЛЧАЛИВАЯ СМЕРТЬ СЕССИИ ─────────────────
    # Транспорт выбирается ПО ПОЛЯМ этого словаря. Если вызывающий написал свой
    # SELECT и забыл, скажем, cf_relay_url, клиент пойдёт НАПРЯМУЮ с host-IP,
    # тогда как другие подсистемы того же аккаунта идут через релей. Одна сессия
    # с двух адресов — AUTH_KEY_DUPLICATED, и Telegram отзывает ключ.
    #
    # Отличаем «поля НЕТ в выборке» от «поле пустое»: пустое — это законное «релей
    # не назначен», а отсутствие ключа — ошибка вызывающего.
    #
    # Сначала пробуем ДОБРАТЬ недостающее из карты транспорта (_ACC_TRANSPORT):
    # переписать все собственные SELECT-ы разом нельзя, а ходить не тем выходом
    # нельзя тем более. Что добрали — тем и пойдём; о чём не знаем — говорим
    # вслух, с именем запроса, который надо чинить. Не падаем: клиент нужен.
    if device is not None:
        _missing = [k for k in ("cf_relay_url", "proxy_id") if k not in d]
        _filled = _fill_transport_fields(d) if _missing else []
        if _filled:
            log.warning(
                "acc=%s: словарь аккаунта без полей транспорта %s — добрали из "
                "карты транспорта. Почините выборку: "
                "database.db.telethon_accounts_query().",
                d.get("id"), ", ".join(_filled),
            )
        _unknown = [k for k in _missing if k not in _filled]
        if _unknown:
            log.error(
                "acc=%s: словарь аккаунта без полей транспорта %s, и в карте "
                "транспорта аккаунта нет — клиент может пойти НЕ ТЕМ путём, чем "
                "остальные подсистемы (риск AUTH_KEY_DUPLICATED). Берите поля из "
                "database.db.telethon_accounts_query().",
                d.get("id"), ", ".join(_unknown),
            )

    # Определяем прокси (может поднять ProxyIsolationError если политика требует
    # прокси, а его нет; low_risk-операции не блокируются)
    proxy = _resolve_client_proxy(d, low_risk=low_risk)

    # Выбор транспорта: если нет аккаунт-bound/TG_PROXY И задан CF relay → используем relay.
    # ПЕР-АККАУНТНЫЙ cf_relay_url (из tg_accounts, раздаётся cf_pool_manager) имеет
    # приоритет над глобальным CF_RELAY_URL — это и даёт «уникальный edge-IP на аккаунт».
    # Аккаунт со своим relay всегда ходит через него (стабильный exit → без рассинхрона IP).
    has_bound_proxy = bool(proxy)  # _resolve_client_proxy вернул не None
    _transport = "bound" if has_bound_proxy else "direct"  # уточняется ниже по ветке
    # Читаем из d, а не из device: d — это словарь ПОСЛЕ добора недостающих
    # транспортных полей. Взять здесь исходный device значило бы пойти напрямую
    # ровно в том случае, ради которого добор и делается.
    acc_relay = ""
    if device:
        acc_relay = str(d.get("cf_relay_url") or "").strip()
    # Требование оператора (повторено трижды): БЕЗ прокси аккаунт работает на РЕАЛЬНОМ
    # host-IP. Глобальный CF_RELAY_URL — это ОБЩИЙ edge-IP пула (не реальный адрес
    # аккаунта) и потенциальный разъезд IP логин↔операция → AUTH_KEY_DUPLICATED.
    # Поэтому глобальный релей НЕ подменяет прямой выход под allow_direct (дефолт);
    # он включается лишь под strict (там host-IP запрещён осознанно). ПЕР-АККАУНТНЫЙ
    # cf_relay_url (явно назначенный пользователем/пулом) honored всегда — это выбор.
    _pol = _effective_proxy_policy(d)
    relay_url = acc_relay or (CF_RELAY_URL if _pol == "strict" else "")

    # local_addr/use_ipv6 — привязка исходящего сокета к своему IPv6 аккаунта.
    local_addr = None
    use_ipv6 = False
    _acc_id = device.get("id") if device else None

    # Подсеть: сначала пер-владелец из приложения (device/кэш), иначе глобальный env.
    _owner_id = device.get("owner_id") if device else None
    _subnet = ""
    if device:
        _subnet = (device.get("ipv6_subnet") or "").strip()
    if not _subnet and _owner_id is not None:
        _subnet = _OWNER_IPV6_SUBNET.get(int(_owner_id), "")
    if not _subnet:
        _subnet = _IPV6_SUBNET

    if not has_bound_proxy and _subnet and _acc_id and not _force_direct:
        # ПРИОРИТЕТ над CF-релеем: IPv6 даёт РЕАЛЬНО уникальный IP на аккаунт
        # (у CF-релея общий edge-IP на пул). Прямое obfuscated-подключение к
        # IPv6-DC Telegram с bind на свой адрес.
        _v6 = _account_ipv6(int(_acc_id), _subnet)
        if _v6:
            local_addr = _v6
            use_ipv6 = True
            connection_cls = ConnectionTcpObfuscated
            effective_proxy = None
            _transport = "ipv6"
            log.debug("acc=%s → уникальный IPv6 %s (без прокси)", _acc_id, _v6)

    if local_addr is not None:
        pass  # транспорт уже выбран (IPv6 direct)
    elif not has_bound_proxy and relay_url and not _force_direct:
        from services.cf_relay import make_cf_relay_connection as _make_relay
        connection_cls = _make_relay(relay_url)
        effective_proxy = None  # relay сам маршрутизирует
        _transport = "relay"
    else:
        connection_cls = ConnectionTcpObfuscated
        if not has_bound_proxy:
            # Нет назначенного прокси, релея, IPv6 → стабильный ПРЯМОЙ host-IP.
            # Публичный free-pool удалён (нестабильные IP блокировали работу).
            # Прокси задаёт пользователь; без него — прямой выход.
            proxy = None
        effective_proxy = proxy
        if not has_bound_proxy:
            _transport = "direct"
        # ── KILL-SWITCH (strict) ──────────────────────────────────────────────
        # Сюда попадаем, только когда все не-host транспорты недоступны (нет
        # аккаунт-прокси/TG_PROXY, нет CF-relay, нет IPv6, пуст free-pool). Если
        # effective_proxy всё ещё None — соединение уйдёт НАПРЯМУЮ с IP хоста.
        # По политике strict это запрещаем (не сливаем реальный адрес); low_risk-
        # чтения не трогаем — им прямой канал разрешён.
        if effective_proxy is None and not low_risk:
            from services.proxy_policy import proxy_decision as _pdec
            _has_assigned = bool(str(d.get("proxy_url") or "").strip()) or bool(d.get("proxy_id"))
            _dec = _pdec(has_proxy_url=_has_assigned, proxy_parsed_ok=False,
                         policy=_effective_proxy_policy(d),
                         enforce=bool(d.get("enforce_proxy")), low_risk=low_risk)
            if _dec == "block":
                # Только ЯВНЫЙ strict (per-owner/env) жёстко запрещает прямой выход.
                raise ProxyIsolationError(
                    "Kill-switch (strict): у аккаунта нет прокси/релея/IPv6 — прямое "
                    "соединение с IP хоста запрещено политикой strict. Назначьте прокси "
                    "аккаунту либо снимите strict (PROXY_POLICY=allow_direct)."
                )
            # Дефолт allow_direct: прямой выход РАЗРЕШЁН как последний резерв, но это
            # риск блокировок (Telegram видит IP хоста) — предупреждаем в лог. Флаг
            # для user-facing предупреждения оператору проставляет слой операции
            # (у аккаунта нет назначенного прокси).
            log.warning(
                "acc=%s connects DIRECT (host IP: нет прокси/релея/IPv6/пула) — риск "
                "блокировок. Назначьте прокси; для жёсткой изоляции — PROXY_POLICY=strict.",
                d.get("id"))

    _extra_kwargs = {}
    if local_addr is not None:
        # Только когда IPv6 реально активен — иначе вызов идентичен прежнему
        # (нулевой риск для существующего поведения/старых версий Telethon).
        _extra_kwargs["use_ipv6"] = use_ipv6
        _extra_kwargs["local_addr"] = local_addr

    _client = TelegramClient(
        StringSession(session_string),
        int(d.get("api_id") or TG_API_ID),
        d.get("api_hash") or TG_API_HASH,
        device_model=d["device_model"],
        system_version=d["system_version"],
        app_version=d["app_version"],
        lang_code=d["lang_code"],
        system_lang_code=d["system_lang_code"],
        connection_retries=1,
        request_retries=1,
        timeout=_CONNECT_TIMEOUT,
        flood_sleep_threshold=0,
        proxy=effective_proxy,
        connection=connection_cls,
        **_extra_kwargs,
    )
    try:
        _client._infragram_transport = _transport
    except Exception:
        pass
    # RAW-коннекторы (десятки функций _make_client(...).connect() мимо
    # connect_client) получают мьютекс сессии автоматически: одну сессию в момент
    # держит ровно один живой коннект. connect_client управляет мьютексом сам
    # (_mutex_managed=True) — его клиенты здесь не оборачиваются во избежание
    # двойного захвата. session_string тут уже расшифрован — _session_key
    # нормализует его к тому же ключу, что и у connect_client (шифр).
    if not _mutex_managed:
        _skey = _session_key(session_string, device)
        if _skey:
            _wrap_client_session_mutex(_client, _skey)
    return _client


def _direct_fallback_ok(transport: str | None, policy: str) -> bool:
    """Можно ли после сбоя коннекта переподключиться НАПРЯМУЮ (host IP).

    Разрешено для безпроксёвых транспортов relay/ipv6: это лишь способы выхода для
    аккаунта БЕЗ назначенного прокси, и если способ недоступен (лежащий/
    несконфигурированный CF-релей, недоступный IPv6) — прямой стабильный host-IP
    лучше несостоявшегося коннекта. Именно так флот и работает «на реальном IP»,
    когда прокси/релей/IPv6 не заданы.

    ЗАПРЕЩЕНО для bound-прокси (сессия привязана к IP прокси — смена IP ломает
    auth key) и для strict-политики (осознанная изоляция реального адреса).
    (Публичный free-pool / транспорт 'pool' удалён.)"""
    return transport in ("relay", "ipv6") and policy != "strict"


async def connect_client(session_string: str = "", device: dict | None = None,
                         action_type: str = "op", low_risk: bool = False,
                         retry_auth_dup: bool = True):
    """Построить и подключить клиента с ФОЛБЭКОМ на прямое подключение.

    Корень жалобы «аккаунты без прокси не стартуют»: у безпроксёвого аккаунта
    транспорт по цепочке падает в free-pool публичных SOCKS5. Пул валидируется на
    скрейпе, но прокси умирают между циклами — и аккаунт намертво залипал на мёртвом
    прокси, коннект падал сетевой ошибкой, операция не выполнялась. Теперь: если шли
    через free-pool ('pool'), политика допускает прямой выход (allow_direct) и коннект
    упал сетевой ошибкой — один раз переподключаемся НАПРЯМУЮ (стабильный host IP).

    Прямой fallback НЕ трогает аккаунты с назначенным прокси (там смена IP ломает
    auth key) и strict-политику (там прямой выход запрещён осознанно).

    AUTH_KEY_DUPLICATED (сессия видится Telegram с двух IP одновременно) —
    ВРЕМЕННЫЙ конфликт: у свежеавторизованного аккаунта первый коннект операции
    может идти с другого IP, чем логин (на логине ещё нет account_id → IPv6/пул
    выбираются иначе), и «лишний» коннект логина/фонового цикла ещё не закрылся.
    Раньше это сразу списывало аккаунт, и оператору приходилось вручную «повторять
    инвайт». Теперь мы делаем это АВТОМАТИЧЕСКИ: короткий бэк-офф + переподключение
    — за 1–2 попытки конфликт снимается сам, и флот стартует без ручного повтора.

    Возвращает подключённого клиента или пробрасывает исходную ошибку."""
    from telethon.errors import AuthKeyDuplicatedError

    # Пре-проверки/диагностика (readiness, membership) вызывают с
    # retry_auth_dup=False: они НЕ захватывают аккаунт, поэтому ретраить конфликт
    # бессмысленно (фоновый коннект никуда не денется), а 29с-ожидание вешает
    # HTTP-запрос до таймаута шлюза. Такие вызовы падают быстро.
    # Процессный мьютекс на сессию: одну сессию в один момент держит ровно один
    # коннект (иначе — AUTH_KEY_DUPLICATED навсегда). Ждём освобождения ограниченно;
    # не дождались → SessionBusyError (транзиентный скип), а НЕ смерть аккаунта.
    _skey = _session_key(session_string, device)
    _wait_budget = _SESSION_ACQUIRE_WAIT_S if retry_auth_dup else 0.0
    _waited = 0.0
    while not _try_acquire_session(_skey):
        if _waited >= _wait_budget:
            raise SessionBusyError(
                f"acc={(device or {}).get('id')}: сессия занята другим подключением "
                f"(ожидание {_wait_budget:.0f}с исчерпано) — повтор позже")
        await asyncio.sleep(min(2.5, _wait_budget - _waited) or 2.5)
        _waited += 2.5

    _backoff = _AUTH_DUP_BACKOFF if retry_auth_dup else ()
    last_dup: Exception | None = None
    _force_direct_retry = False  # после duplicated на безпроксёвом транспорте
    try:
      for _attempt in range(len(_backoff) + 1):
        client = _make_client(session_string, device, low_risk=low_risk,
                              _force_direct=_force_direct_retry, _mutex_managed=True)
        try:
            await _connect_and_track(client, device, action_type)
            _bind_session_release(client, _skey)
            return client
        except AuthKeyDuplicatedError as e:
            last_dup = e
            _transport = getattr(client, "_infragram_transport", None)
            try:
                await client.disconnect()
            except Exception:
                pass
            if _attempt < len(_backoff):
                _delay = _backoff[_attempt]
                # Для БЕЗПРОКСЁВОГО транспорта (релей/пул/IPv6) частая причина
                # duplicated — скачущий/общий exit-IP. На ретрае уходим на прямой
                # стабильный host-IP. Bound-прокси/strict не трогаем.
                if _direct_fallback_ok(_transport, _effective_proxy_policy(device or {})):
                    _force_direct_retry = True
                log.warning(
                    "acc=%s: AUTH_KEY_DUPLICATED (сессия с двух IP, транспорт '%s') — "
                    "авто-ретрай через %.0fс%s (попытка %d/%d)",
                    (device or {}).get("id"), _transport, _delay,
                    " на прямой host-IP" if _force_direct_retry else "",
                    _attempt + 1, len(_backoff) + 1,
                )
                await asyncio.sleep(_delay)
                continue
            # Исчерпали ретраи — пробрасываем (слой операции покажет подсказку).
            raise
        except (OSError, ConnectionError, asyncio.TimeoutError) as e:
            if isinstance(e, ProxyIsolationError):
                raise  # изоляция bound-прокси — прямой выход недопустим
            transport = getattr(client, "_infragram_transport", None)
            policy = _effective_proxy_policy(device or {})
            if not _direct_fallback_ok(transport, policy):
                raise
            try:
                await client.disconnect()
            except Exception:
                pass
            log.warning(
                "acc=%s: транспорт '%s' недоступен (%s) — fallback на ПРЯМОЕ подключение "
                "с host-IP (allow_direct)",
                (device or {}).get("id"), transport, str(e)[:80],
            )
            # _force_direct: минуем релей/IPv6/пул — идём стабильным host-IP,
            # иначе fallback снова уткнётся в тот же нерабочий релей.
            client = _make_client(session_string, device, low_risk=low_risk,
                                  _no_pool=True, _force_direct=True, _mutex_managed=True)
            await _connect_and_track(client, device, action_type)
            _bind_session_release(client, _skey)
            return client
      # Теоретически недостижимо (цикл либо возвращает, либо пробрасывает).
      if last_dup:
        raise last_dup
      raise RuntimeError("connect_client: не удалось подключиться")
    except BaseException:
        # НИ ОДНОГО живого клиента не вернули → освобождаем сессию сейчас
        # (успешный возврат освобождает её через обёртку disconnect).
        _release_session(_skey)
        raise


async def start_login(
    phone: str,
    proxy_url: str | None = None,
    manufacturer: str | None = None,
    app_version: str | None = None,
) -> tuple[str, str]:
    """Начинает авторизацию по номеру телефона.

    Device fingerprint's locale is derived from the phone's own calling code
    (country_code_from_phone) so it actually matches where the number is
    from, instead of always defaulting to ru-RU. proxy_url, when given, binds
    this login (and therefore the saved account) to that proxy — callers
    doing mass/auto-registration should pass a per-registration proxy (see
    pick_registration_proxy) so numbers from different countries/batches
    don't all connect through the same IP. manufacturer/app_version pin the
    emulated device to the owner's saved "Генератор параметров" preference
    (see auto_registrar.py) instead of a fully random pick.

    Возвращает (phone_code_hash, delivery_hint) где delivery_hint — строка о способе доставки.
    """
    from telethon.errors import FloodWaitError

    if not TG_API_ID or not TG_API_HASH:
        raise ValueError(
            "TG_API_ID / TG_API_HASH не настроены. Укажите в переменных среды."
        )
    device = generate_device_fingerprint(
        country_code_from_phone(phone), manufacturer=manufacturer, app_version=app_version
    )
    if proxy_url:
        device["proxy_url"] = proxy_url
    # Телефон в device → _make_client при фолбэке в пул залипает по нему. Так ЛОГИН
    # и будущие ОПЕРАЦИИ этого аккаунта берут ОДИН и тот же pool-прокси (один exit IP),
    # и свежеавторизованная сессия не падает в AUTH_KEY_DUPLICATED на первой операции.
    device["phone"] = phone
    _pending_device[phone] = device
    client = _make_client("", device)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await asyncio.wait_for(
            client.send_code_request(phone), timeout=_CONNECT_TIMEOUT
        )
    except FloodWaitError:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в start_login")
        raise
    except Exception as e:
        log.warning("start_login failed: %s", e)
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в start_login")
        raise
    _pending[phone] = client

    # Determine where the code was sent so handlers can tell the user
    type_name = type(result.type).__name__ if result.type else ""
    if "App" in type_name:
        delivery_hint = "📱 Код отправлен в приложение Telegram"
    elif "Sms" in type_name:
        delivery_hint = "💬 Код отправлен по SMS"
    elif "Call" in type_name or "Flash" in type_name:
        delivery_hint = "📞 Код придёт звонком на номер"
    else:
        delivery_hint = "📱 Код отправлен (проверьте приложение Telegram или SMS)"

    return result.phone_code_hash, delivery_hint


async def resend_code(phone: str, phone_code_hash: str) -> tuple[str, str]:
    """Resend code via next available method (usually SMS if app was first).
    Returns (new_phone_code_hash, delivery_hint).
    """
    from telethon.tl.functions.auth import ResendCodeRequest
    from telethon.errors import FloodWaitError

    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        result = await asyncio.wait_for(
            client(
                ResendCodeRequest(phone_number=phone, phone_code_hash=phone_code_hash)
            ),
            timeout=_CONNECT_TIMEOUT,
        )
    except FloodWaitError:
        raise
    type_name = type(result.type).__name__ if result.type else ""
    if "Sms" in type_name:
        hint = "💬 Код отправлен по SMS"
    elif "Call" in type_name or "Flash" in type_name:
        hint = "📞 Код придёт звонком"
    elif "App" in type_name:
        hint = "📱 Код отправлен в приложение Telegram"
    else:
        hint = "💬 Код выслан повторно (SMS или звонок)"
    return result.phone_code_hash, hint


async def confirm_code(phone: str, code: str, phone_code_hash: str):
    """Confirm SMS/TG code. Returns client or 'need_2fa'."""
    from telethon.errors import (
        PhoneCodeInvalidError,
        PhoneCodeExpiredError,
        SessionPasswordNeededError,
    )

    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        return client
    except SessionPasswordNeededError:
        return "need_2fa"
    except PhoneCodeExpiredError:
        raise ValueError(
            "Код истёк — запросите новый код через кнопку «Отправить повторно»."
        )
    except PhoneCodeInvalidError:
        raise ValueError("Неверный код — проверьте и введите снова.")


async def confirm_2fa(phone: str, password: str):
    """Complete 2FA login. Returns client."""
    from telethon.errors import PasswordHashInvalidError

    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия истекла — начните заново.")
    try:
        await client.sign_in(password=password)
        return client
    except PasswordHashInvalidError:
        raise ValueError("Неверный пароль 2FA.")


async def get_session_string(client) -> str:
    return client.session.save()


# ── Session Import Helpers ─────────────────────────────────────────────────────


async def import_from_session_string(session_string: str) -> tuple[str, dict]:
    """Validate a Telethon StringSession and return (session_str, info).
    Raises ValueError if the session is invalid or unauthorized.
    Error messages are user-friendly and specific to the failure reason.
    """
    session_string = session_string.strip()
    if not session_string or len(session_string) < 20:
        raise ValueError("Строка сессии слишком короткая.")

    # Use a realistic Android fingerprint during import validation.
    # Telethon's bare default ("PC 64bit" / version string) is a known
    # Telegram anti-abuse signal — it must never reach Telegram servers.
    device = generate_device_fingerprint()
    client = _make_client(session_string, device)
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise ValueError(
                "Таймаут подключения к Telegram. Проверьте настройки сети/прокси."
            )
        except Exception as conn_exc:
            err = str(conn_exc)
            if "AuthKeyUnregisteredError" in type(conn_exc).__name__ or "auth_key" in err.lower():
                raise ValueError(
                    "Ключ сессии не зарегистрирован в Telegram. Сессия недействительна."
                )
            raise ValueError(f"Ошибка подключения к Telegram: {err[:200]}")

        try:
            is_auth = await asyncio.wait_for(client.is_user_authorized(), timeout=15.0)
        except Exception as e:
            log.warning("import_from_session_string: is_user_authorized failed: %s", e)
            is_auth = False

        if not is_auth:
            raise ValueError("Сессия не авторизована или истекла. Требуется повторный вход.")

        try:
            me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        except asyncio.TimeoutError:
            raise ValueError("Таймаут получения данных аккаунта. Повторите попытку.")
        except Exception as me_exc:
            err = str(me_exc)
            err_type = type(me_exc).__name__
            if "UserDeactivated" in err_type or "deactivated" in err.lower():
                raise ValueError("Аккаунт деактивирован (удалён) пользователем.")
            if "UserBannedInChannel" in err_type or "banned" in err.lower():
                raise ValueError("Аккаунт заблокирован Telegram.")
            if "SessionRevoked" in err_type or "revoked" in err.lower():
                raise ValueError("Сессия отозвана — аккаунт вышел на другом устройстве.")
            raise ValueError(f"Не удалось получить данные аккаунта: {err[:200]}")

        if me is None:
            raise ValueError("Сессия не вернула данные аккаунта. Возможно, аккаунт удалён.")

        info = {
            "tg_user_id": me.id,
            "phone": getattr(me, "phone", "") or f"id:{me.id}",
            "first_name": getattr(me, "first_name", "") or "",
            "username": getattr(me, "username", "") or "",
            **device,
        }
        return session_string, info
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в import_from_session_string")


async def import_from_pyrogram_json(json_str: str) -> tuple[str, dict]:
    """Convert a Pyrogram JSON session to Telethon StringSession.

    Accepted JSON fields: dc_id, auth_key (base64), user_id (optional).
    Converts auth_key + dc_id to a Telethon StringSession and validates it.
    """
    import json as _json
    import struct
    import base64
    from ipaddress import IPv4Address

    try:
        data = _json.loads(json_str)
    except Exception as e:
        log.warning("import_from_pyrogram_json: json parse failed: %s", e)
        raise ValueError("Некорректный JSON. Проверьте формат.")

    dc_id = int(data.get("dc_id") or 2)
    auth_key_raw = data.get("auth_key", "")
    if not auth_key_raw:
        raise ValueError("Поле auth_key не найдено в JSON.")

    try:
        auth_key = base64.b64decode(auth_key_raw + "==")
    except Exception as e:
        log.warning("import_from_pyrogram_json: base64 decode failed: %s", e)
        raise ValueError("Не удалось декодировать auth_key (ожидается base64).")

    if len(auth_key) != 256:
        raise ValueError(f"Неверная длина auth_key: {len(auth_key)}, нужно 256 байт.")

    # Production DC server IPs
    DC_IPS: dict[int, str] = {
        1: "149.154.175.53",
        2: "149.154.167.51",
        3: "149.154.175.100",
        4: "149.154.167.91",
        5: "91.108.56.130",
    }
    ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed
    packed = struct.pack(">B4sH256s", dc_id, ip_bytes, 443, auth_key)
    session_string = "1" + base64.urlsafe_b64encode(packed).decode()

    return await import_from_session_string(session_string)


async def import_from_session_file(
    session_bytes: bytes, filename: str = ""
) -> tuple[str, dict]:
    """Convert a Telethon .session SQLite file to StringSession.

    The .session file is a SQLite database with a 'sessions' table:
    dc_id INTEGER, server_address TEXT, port INTEGER, auth_key BLOB
    """
    import sqlite3
    import struct
    import base64
    import tempfile
    import os
    from ipaddress import IPv4Address

    # Write bytes to temp file for sqlite3 to open
    tmp = tempfile.NamedTemporaryFile(suffix=".session", delete=False)
    try:
        tmp.write(session_bytes)
        tmp.flush()
        tmp.close()

        try:
            conn = sqlite3.connect(tmp.name)
            cur = conn.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"Файл не является корректным .session файлом: {e}")

        if not row:
            raise ValueError("Таблица sessions пустая — сессия не авторизована.")

        dc_id, server_address, port, auth_key_bytes = row
        if not auth_key_bytes or len(auth_key_bytes) != 256:
            raise ValueError(
                f"Некорректный auth_key в сессии (длина: {len(auth_key_bytes) if auth_key_bytes else 0}, нужно 256)."
            )

        # Build StringSession in Telethon format (version 1)
        try:
            ip_bytes = IPv4Address(server_address).packed
        except Exception as e:
            log.warning("import_from_session_file: IPv4Address parse failed: %s", e)
            DC_IPS = {
                1: "149.154.175.53",
                2: "149.154.167.51",
                3: "149.154.175.100",
                4: "149.154.167.91",
                5: "91.108.56.130",
            }
            ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed

        packed = struct.pack(
            ">B4sH256s", dc_id, ip_bytes, int(port or 443), bytes(auth_key_bytes)
        )
        session_string = "1" + base64.urlsafe_b64encode(packed).decode()

    finally:
        try:
            os.unlink(tmp.name)
        except Exception as e:
            log.warning("import_from_session_file: cleanup tmp: %s", e)

    return await import_from_session_string(session_string)


async def convert_session_file_to_string(session_bytes: bytes) -> str:
    """Convert a Telethon .session SQLite file bytes to a StringSession string.

    Unlike import_from_session_file, this does NOT connect to Telegram.
    Returns the raw StringSession string for use in batch import.
    Raises ValueError on invalid file.
    """
    import sqlite3
    import struct
    import base64
    import tempfile
    import os
    from ipaddress import IPv4Address

    tmp = tempfile.NamedTemporaryFile(suffix=".session", delete=False)
    try:
        tmp.write(session_bytes)
        tmp.flush()
        tmp.close()
        try:
            conn = sqlite3.connect(tmp.name)
            cur = conn.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions LIMIT 1"
            )
            row = cur.fetchone()
            conn.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"Не является .session файлом: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception as e:
            log.warning("convert_session_file_to_string: cleanup tmp: %s", e)

    if not row:
        raise ValueError("Таблица sessions пустая — сессия не авторизована")
    dc_id, server_address, port, auth_key_bytes = row
    if not auth_key_bytes or len(auth_key_bytes) != 256:
        raise ValueError(
            f"Некорректный auth_key (длина: {len(auth_key_bytes) if auth_key_bytes else 0})"
        )
    try:
        ip_bytes = IPv4Address(server_address).packed
    except Exception as e:
        log.warning("convert_session_file_to_string: IPv4Address parse failed: %s", e)
        DC_IPS = {
            1: "149.154.175.53",
            2: "149.154.167.51",
            3: "149.154.175.100",
            4: "149.154.167.91",
            5: "91.108.56.130",
        }
        ip_bytes = IPv4Address(DC_IPS.get(dc_id, DC_IPS[2])).packed
    packed = struct.pack(
        ">B4sH256s", dc_id, ip_bytes, int(port or 443), bytes(auth_key_bytes)
    )
    return "1" + base64.urlsafe_b64encode(packed).decode()


async def import_from_tdata(tdata_path: str) -> tuple[str, dict]:
    """Convert a TDesktop tdata directory to Telethon StringSession.

    Пробует opentele (если установлен), иначе использует нативный конвертер.
    """
    # ── Попытка 1: opentele (если доступен) ──────────────────────────────────
    try:
        from telethon.sessions import StringSession as _SS

        td_module = importlib.import_module("opentele.td")
        api_module = importlib.import_module("opentele.api")
        TDesktop = getattr(td_module, "TDesktop")
        UseCurrentSession = getattr(api_module, "UseCurrentSession")

        try:
            td = TDesktop(tdata_path)
        except Exception as e:
            raise ValueError(f"Не удалось загрузить tdata через opentele: {e}")

        if not td.isLoaded():
            raise ValueError("tdata не загружены (opentele). Проверьте папку tdata.")

        try:
            client = await td.ToTelethon(session=_SS(), flag=UseCurrentSession)
        except Exception as e:
            raise ValueError(f"Ошибка конвертации tdata → Telethon (opentele): {e}")

        try:
            await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
            if not await client.is_user_authorized():
                raise ValueError("Сессия из tdata (opentele) не авторизована.")
            session_str = client.session.save()
            me = await client.get_me()
            info = {
                "tg_user_id": me.id,
                "phone": getattr(me, "phone", "") or f"id:{me.id}",
                "first_name": getattr(me, "first_name", "") or "",
                "username": getattr(me, "username", "") or "",
            }
            return session_str, info
        finally:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "Сбой в import_from_tdata (opentele disconnect)")
    except ImportError:
        pass  # opentele не установлен — используем нативный конвертер

    # ── Попытка 2: нативный конвертер (pycryptodome) ──────────────────────────
    from services.tdata_converter import convert_tdata, check_pycryptodome

    if not check_pycryptodome():
        raise ImportError(
            "Конвертация tdata недоступна: ни opentele, ни pycryptodome не установлены.\n"
            "Используйте String Session или .session файл."
        )

    try:
        sessions = convert_tdata(tdata_path)
    except Exception as e:
        raise ValueError(f"Ошибка конвертации tdata: {e}")

    if not sessions:
        raise ValueError(
            "tdata конвертирован, но аккаунты не найдены. "
            "Возможно tdata защищён паролем или использует нестандартный формат. "
            "Попробуйте импорт через String Session."
        )

    # Берём первую сессию и проверяем через Telegram
    session_str = sessions[0]["session_str"]

    # Use a realistic Android fingerprint — same reason as import_from_session_string.
    device = generate_device_fingerprint()
    client = _make_client(session_str, device)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        if me is None:
            raise ValueError(
                "Сессия из tdata не авторизована в Telegram. "
                "Возможно, tdata устарел или аккаунт был переавторизован."
            )
        session_str = client.session.save()
        info = {
            "tg_user_id": me.id,
            "phone": getattr(me, "phone", "") or f"id:{me.id}",
            "first_name": getattr(me, "first_name", "") or "",
            "username": getattr(me, "username", "") or "",
            **device,
        }
        return session_str, info
    except asyncio.TimeoutError:
        raise ValueError(
            "Таймаут подключения через tdata. "
            "Проверьте подключение к Telegram или используйте String Session."
        )
    except Exception as e:
        err = str(e)
        if "AUTH_KEY" in err or "SESSION_REVOKED" in err:
            raise ValueError(
                "Ключ сессии из tdata недействителен — аккаунт был переавторизован или сессия отозвана. "
                "Экспортируйте свежий tdata или используйте String Session."
            )
        raise ValueError(f"Ошибка при подключении через tdata: {err[:200]}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой отключения в import_from_tdata")


def _find_tdata_root(extract_dir: str) -> str | None:
    """Найти корень tdata (папку с файлом key_datas) в распакованном архиве."""
    import os
    for root, dirs, files in os.walk(extract_dir):
        if "key_datas" in files:
            return root
        depth = root[len(extract_dir):].count(os.sep)
        if depth >= 3:
            dirs.clear()
    return None


async def import_tdata_from_zip_bytes(zip_bytes: bytes) -> tuple[str, dict]:
    """ZIP-архив папки tdata (в байтах) → Telethon StringSession + info.

    Инкапсулирует безопасную распаковку (защита от zip-bomb и path-traversal),
    поиск корня tdata и конвертацию. Единая точка для веб-API и бота.
    Бросает ValueError с человекочитаемой причиной.
    """
    import os as _os
    import tempfile
    import zipfile

    _MAX_UNCOMPRESSED = 200 * 1024 * 1024  # 200 MB
    _MAX_FILES = 5_000

    tmp_dir = tempfile.mkdtemp(prefix="tdata_web_")
    zip_path = _os.path.join(tmp_dir, "tdata.zip")
    extract_dir = _os.path.join(tmp_dir, "extracted")
    _os.makedirs(extract_dir, exist_ok=True)
    try:
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.infolist()
                if len(members) > _MAX_FILES:
                    raise ValueError(f"Слишком много файлов в архиве (>{_MAX_FILES}).")
                if sum(i.file_size for i in members) > _MAX_UNCOMPRESSED:
                    raise ValueError("Архив слишком большой в распакованном виде (>200 МБ).")
                for member in members:
                    fname = member.filename.replace("\\", "/")
                    if _os.path.isabs(fname) or ".." in fname.split("/"):
                        raise ValueError("Подозрительные пути в архиве (path traversal). Отклонено.")
                zf.extractall(extract_dir)
        except zipfile.BadZipFile:
            raise ValueError("Файл повреждён или не является ZIP-архивом.")

        tdata_path = _find_tdata_root(extract_dir)
        if not tdata_path:
            raise ValueError("Папка tdata (с файлом key_datas) не найдена в архиве.")
        return await import_from_tdata(tdata_path)
    finally:
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            log_exc_swallow(log, "import_tdata_from_zip_bytes cleanup")


async def get_client_info_and_session(phone: str) -> tuple[str, dict]:
    """Get session string + user info from a pending login. Call after confirm_code/confirm_2fa."""
    client = _pending.get(phone)
    if not client:
        raise ValueError("Сессия не найдена — начните авторизацию заново.")
    session_str = client.session.save()
    me = await client.get_me()
    info = {
        "tg_user_id": me.id,
        "phone": me.phone or phone,
        "first_name": me.first_name or "",
        "username": me.username or "",
        **_pending_device.get(phone, {}),
    }
    return session_str, info


async def cleanup_pending(phone: str) -> None:
    _pending_device.pop(phone, None)
    client = _pending.pop(phone, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в cleanup_pending")


# ── QR Login ──────────────────────────────────────────────────────────────────


async def start_qr_login(user_id: int) -> bytes:
    """Start QR code login. Returns PNG image bytes.

    Keeps a connected client in _pending_qr[user_id].
    Call wait_qr_login() in a background task to detect scan.
    """
    import io
    import qrcode

    await cleanup_qr_pending(user_id)

    device = generate_device_fingerprint()
    client = _make_client("", device)
    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
    qr = await client.qr_login()
    _pending_qr[user_id] = (client, qr, device)

    img = qrcode.make(qr.url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def wait_qr_login(user_id: int, timeout: float = 120.0) -> tuple[str, dict]:
    """Block until user scans QR code or timeout. Returns (session_str, info).

    Raises asyncio.TimeoutError if not scanned in time.
    Raises SessionPasswordNeededError if account requires 2FA.
    """
    from telethon.errors import SessionPasswordNeededError

    entry = _pending_qr.get(user_id)
    if not entry:
        raise ValueError("QR сессия не найдена — начните заново.")
    client, qr, device = entry
    try:
        await asyncio.wait_for(qr.wait(), timeout=timeout)
    except SessionPasswordNeededError:
        # Caller must handle 2FA separately; client stays in _pending_qr
        raise

    me = await client.get_me()
    session_str = client.session.save()
    info = {
        "tg_user_id": me.id,
        "phone": getattr(me, "phone", "") or f"id:{me.id}",
        "first_name": getattr(me, "first_name", "") or "",
        "username": getattr(me, "username", "") or "",
        **device,
    }
    return session_str, info


async def confirm_qr_2fa(user_id: int, password: str) -> tuple[str, dict]:
    """Finish QR login that required 2FA. Returns (session_str, info)."""
    from telethon.errors import PasswordHashInvalidError

    entry = _pending_qr.get(user_id)
    if not entry:
        raise ValueError("QR сессия не найдена — начните заново.")
    client, _, device = entry
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        raise ValueError("Неверный пароль 2FA.")

    me = await client.get_me()
    session_str = client.session.save()
    info = {
        "tg_user_id": me.id,
        "phone": getattr(me, "phone", "") or f"id:{me.id}",
        "first_name": getattr(me, "first_name", "") or "",
        "username": getattr(me, "username", "") or "",
        **device,
    }
    return session_str, info


async def cleanup_qr_pending(user_id: int) -> None:
    entry = _pending_qr.pop(user_id, None)
    if entry:
        client, *_ = entry
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в cleanup_qr_pending")


async def get_account_info(session_string: str, _acc: dict | None = None) -> dict:
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await client.get_me()
        return {
            "tg_user_id": me.id,
            "phone": me.phone or "",
            "first_name": me.first_name or "",
            "username": me.username or "",
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_account_info")


async def get_dialogs(
    session_string: str, limit: int | None = 50, offset: int = 0, _acc: dict | None = None
) -> list[dict]:
    """Возвращает каналы и группы аккаунта с поддержкой пагинации."""
    if not session_string:
        log.warning("get_dialogs: session_str отсутствует — сессия недоступна")
        return []
    from telethon.tl.types import Channel, Chat

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "dialogs")
        from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
        dialogs = []
        _iter = client.iter_dialogs(limit=limit, offset_id=offset)
        while True:
            try:
                dialog = await _iter.__anext__()
            except StopAsyncIteration:
                break
            except (ChannelPrivateError, ChatAdminRequiredError):
                continue
            except Exception as e:
                log.debug("get_dialogs: iter_dialogs skip: %s", e)
                continue
            try:
                entity = dialog.entity
            except Exception as e:
                log.debug("get_dialogs: dialog.entity error: %s", e)
                continue
            if isinstance(entity, (Channel, Chat)):
                # Права аккаунта в этом канале/группе — чтобы отличать «мою
                # инфраструктуру» (создатель/админ) от чужих подписок (участник).
                _is_creator = bool(getattr(entity, "creator", False))
                _admin_rights = getattr(entity, "admin_rights", None)
                dialogs.append(
                    {
                        "id": entity.id,
                        "title": entity.title,
                        "type": (
                            "channel"
                            if isinstance(entity, Channel)
                            and getattr(entity, "broadcast", False)
                            else "megagroup"
                            if isinstance(entity, Channel)
                            and getattr(entity, "megagroup", False)
                            else "supergroup"
                            if isinstance(entity, Channel)
                            else "group"
                        ),
                        "members": getattr(entity, "participants_count", 0) or 0,
                        "username": getattr(entity, "username", "") or "",
                        "access_hash": getattr(entity, "access_hash", 0) or 0,
                        "is_creator": _is_creator,
                        "is_admin": _is_creator or _admin_rights is not None,
                    }
                )
        return dialogs
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "dialogs")
        log.warning("get_dialogs: connect timeout — proxy may be dead")
        return []
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "dialogs")
        log.warning("get_dialogs: network error (proxy?): %s", e)
        return []
    except Exception as e:
        from telethon.errors import (
            AuthKeyUnregisteredError,
            SessionRevokedError,
            UserDeactivatedBanError,
            UserDeactivatedError,
        )
        if isinstance(
            e,
            (AuthKeyUnregisteredError, SessionRevokedError,
             UserDeactivatedBanError, UserDeactivatedError),
        ) or is_dead_session_error(str(e)):
            log.warning(
                "get_dialogs: dead session acc=%s — %s",
                (_acc or {}).get("id", "?"),
                type(e).__name__,
            )
            raise  # re-raise so callers can mark account as session_expired
        log.exception("get_dialogs error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_dialogs")


def _pick_free_filter_id(used_ids) -> int:
    """Свободный id папки в клиенте (2..255; 0/1 — All/Archive, зарезервированы).

    Чистая функция — единственная часть создания папки, которую можно проверить
    без Telegram. Возвращает наименьший свободный id; если все заняты — 0
    (сигнал «места нет», исполнитель отдаёт понятную ошибку).
    """
    used = set()
    for i in used_ids or []:
        try:
            used.add(int(i))
        except (TypeError, ValueError):
            continue
    for candidate in range(2, 256):
        if candidate not in used:
            return candidate
    return 0


async def create_shared_folder_link(
    session_string: str, title: str, chat_ids: list[int],
    _acc: dict | None = None, existing_filter_id: int | None = None,
) -> dict:
    """Создать общую папку из указанных чатов и экспортировать chatlist-ссылку.

    Возвращает {ok, invite_link, slug, filter_id, error, error_kind}. error_kind:
    'premium' — нужна Telegram Premium на аккаунте (Telegram требует Premium для
    ШАРИНГА папок), 'auth' — сессия мертва, 'flood' — FloodWait, 'peer' — чат
    недоступен аккаунту, 'other' — прочее.

    ВНИМАНИЕ: сетевые типы Telethon (DialogFilterChatlist, chatlists.*) зависят
    от слоя API и здесь НЕ покрыты юнит-тестами — заглушка пула их не связывает.
    Требует проверки на живом флоте (см. CLAUDE.md про e2e). Логика вокруг
    (выбор filter_id, классификация ошибок) чистая и проверяется.
    """
    from telethon import functions, types
    from telethon.errors import FloodWaitError

    if not chat_ids:
        return {"ok": False, "error": "пустой набор чатов", "error_kind": "other"}

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        async def _work():
            # 1) input-peer для каждого чата; недоступные — отсеиваем с причиной,
            #    а не роняем весь экспорт.
            peers = []
            skipped = 0
            for cid in chat_ids:
                try:
                    peers.append(await client.get_input_entity(int(cid)))
                except Exception:
                    skipped += 1
            if not peers:
                return {"ok": False, "error": "ни один чат недоступен аккаунту",
                        "error_kind": "peer"}

            # 2) свободный filter_id (или переиспользуем папку той же связки).
            fid = int(existing_filter_id) if existing_filter_id else 0
            if not fid:
                try:
                    existing = await client(functions.messages.GetDialogFiltersRequest())
                    used = [getattr(f, "id", 0)
                            for f in getattr(existing, "filters", existing) or []]
                except Exception:
                    used = []
                fid = _pick_free_filter_id(used)
            if not fid:
                return {"ok": False,
                        "error": "в аккаунте не осталось свободных папок",
                        "error_kind": "other"}

            # 3) создаём/обновляем папку как chatlist (шаринг-совместимую).
            #    title в новых слоях — TextWithEntities, в старых — str; пробуем
            #    богатый тип, при рассинхроне слоя падаем на строку.
            try:
                _title_obj = types.TextWithEntities(text=title, entities=[])
            except Exception:
                _title_obj = title
            try:
                await client(functions.messages.UpdateDialogFilterRequest(
                    id=fid,
                    filter=types.DialogFilterChatlist(
                        id=fid, title=_title_obj,
                        pinned_peers=[], include_peers=peers, emoticon=None)))
            except TypeError:
                # Старый слой: DialogFilterChatlist со строковым title.
                await client(functions.messages.UpdateDialogFilterRequest(
                    id=fid,
                    filter=types.DialogFilterChatlist(
                        id=fid, title=title,
                        pinned_peers=[], include_peers=peers, emoticon=None)))

            # 4) экспорт ссылки-приглашения к папке.
            res = await client(functions.chatlists.ExportChatlistInviteRequest(
                chatlist=types.InputChatlistDialogFilter(filter_id=fid),
                title=title, peers=peers))
            link = getattr(getattr(res, "invite", None), "url", "") or ""
            slug = link.rstrip("/").split("/")[-1] if link else ""
            return {"ok": bool(link), "invite_link": link, "slug": slug,
                    "filter_id": fid, "skipped_peers": skipped,
                    "error": None if link else "Telegram не вернул ссылку",
                    "error_kind": None if link else "other"}

        return await asyncio.wait_for(_work(), timeout=_OP_TIMEOUT)
    except FloodWaitError as e:
        return {"ok": False, "error": f"FloodWait {getattr(e,'seconds','?')}с",
                "error_kind": "flood"}
    except Exception as e:
        low = str(e).lower()
        if "premium" in low or "chatlists.chatlist_invites" in low or "user_premium" in low:
            kind = "premium"
        elif any(x in low for x in ("auth", "unauthorized", "key is not registered")):
            kind = "auth"
        else:
            kind = "other"
        return {"ok": False, "error": str(e)[:200], "error_kind": kind}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "create_shared_folder_link: disconnect")


async def scan_owned_assets(session_string: str, _acc: dict | None = None) -> dict:
    """Scan account for channels/groups where it's admin or creator.

    Returns {'channels': [...], 'groups': [...], 'error': str|None}
    Each item: {id, title, username, members, is_creator, access_hash}
    """
    from telethon.tl.types import Channel

    client = _make_client(session_string, _acc)
    channels: list[dict] = []
    groups: list[dict] = []
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        async def _collect():
            from telethon.errors import ChannelPrivateError, ChatAdminRequiredError

            _ch, _gr = [], []
            # Use manual __anext__ so ChannelPrivateError from the iterator
            # itself (thrown mid-batch) skips that dialog instead of killing
            # the entire scan and discarding already-collected results.
            _iter = client.iter_dialogs(limit=300)
            while True:
                try:
                    dialog = await _iter.__anext__()
                except StopAsyncIteration:
                    break
                except (ChannelPrivateError, ChatAdminRequiredError):
                    continue
                except Exception as e:
                    log.debug("scan_owned_assets: iter_dialogs skip: %s", e)
                    continue
                try:
                    entity = dialog.entity
                except (ChannelPrivateError, ChatAdminRequiredError):
                    continue
                except Exception as e:
                    log.debug("scan_owned_assets: dialog.entity error: %s", e)
                    continue
                if isinstance(entity, Channel):
                    is_creator = getattr(entity, "creator", False)
                    admin_rights = getattr(entity, "admin_rights", None)
                    if not (is_creator or admin_rights is not None):
                        continue
                    is_broadcast = getattr(entity, "broadcast", False)
                    item = {
                        "id": entity.id,
                        "title": entity.title or "",
                        "username": getattr(entity, "username", "") or "",
                        "members": getattr(entity, "participants_count", 0) or 0,
                        "is_creator": is_creator,
                        # Сюда попадают только создатель/админ → инфраструктура наша.
                        "is_admin": True,
                        "type": "channel" if is_broadcast else (
                            "megagroup" if getattr(entity, "megagroup", False) else "supergroup"
                        ),
                        "access_hash": getattr(entity, "access_hash", 0) or 0,
                    }
                    if is_broadcast:
                        _ch.append(item)
                    else:
                        _gr.append(item)
            return _ch, _gr

        channels, groups = await asyncio.wait_for(_collect(), timeout=_OP_TIMEOUT)
        return {"channels": channels, "groups": groups, "error": None}
    except Exception as e:
        err_str = str(e)
        err_low = err_str.lower()
        _is_session = any(
            x in err_low
            for x in (
                "auth",
                "authkey",
                "unauthorized",
                "key is not registered",
                "registered in the system",
                "auth_key",
            )
        )
        if _is_session:
            log.warning("scan_owned_assets session dead: %s", e)
        else:
            log.exception("scan_owned_assets error: %s", e)
        return {"channels": [], "groups": [], "error": err_str[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в _collect")


async def send_message_via_account(
    session_string: str, chat_id: int, text: str, _acc: dict | None = None
) -> bool:
    """Отправляет сообщение через личный аккаунт. Возвращает True при успехе.

    Raises FloodWaitError, AuthKeyUnregisteredError, SessionRevokedError so callers
    can apply cooldowns or mark the account dead.  All other errors return False.
    """
    from telethon.errors import (
        FloodWaitError,
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    )

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "send_message")
        await asyncio.wait_for(client.send_message(chat_id, text), timeout=_OP_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "send_message")
        log.warning(
            "send_message_via_account: timeout acc=%s", (_acc or {}).get("id", "?")
        )
        return False
    except FloodWaitError:
        # Re-raise so the caller can apply a proper cooldown
        raise
    except (
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    ):
        # Re-raise dead-session errors so callers can deactivate the account
        raise
    except Exception as e:
        if is_dead_session_error(str(e)):
            raise AuthKeyUnregisteredError(request=None) from e
        log.exception("send_message error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в send_message_via_account")


async def send_media_via_account(
    session_string: str, chat_id, media_url: str | None = None, caption: str = "",
    _acc: dict | None = None,
    *,
    media_bytes: bytes | None = None,
    media_filename: str | None = None,
    uniquify: bool = False,
) -> bool:
    """Отправляет медиа (фото/видео/док) с подписью через личный аккаунт.

    Два источника медиа:
      • `media_url` — Telethon сам скачивает файл (URL должен быть заранее
        провалидирован вызывающей стороной, SSRF-гард на уровне API);
      • `media_bytes` — готовые байты (для массовой рассылки: скачали один раз,
        уникализируем под каждого получателя). При `uniquify=True` каждая копия
        делается пиксельно/байт-различной (анти-детект, см. media_uniquifier).

    Обработка ошибок как в send_message_via_account (флуд/dead-session
    пробрасываются).
    """
    from telethon.errors import (
        FloodWaitError,
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    )

    if media_bytes is not None:
        payload = media_bytes
        if uniquify:
            from services import media_uniquifier
            payload = media_uniquifier.uniquify(media_bytes, filename=media_filename or "")
        import io as _io
        _fobj = _io.BytesIO(payload)
        _fobj.name = media_filename or "media.jpg"
        send_target: Any = _fobj
    else:
        send_target = media_url

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await asyncio.wait_for(
            client.send_file(chat_id, file=send_target, caption=caption or None),
            timeout=max(_OP_TIMEOUT, 60),
        )
        return True
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "send_media")
        log.warning("send_media_via_account: timeout acc=%s", (_acc or {}).get("id", "?"))
        return False
    except FloodWaitError:
        raise
    except (
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    ):
        raise
    except Exception as e:
        if is_dead_session_error(str(e)):
            raise AuthKeyUnregisteredError(request=None) from e
        log.exception("send_media error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в send_media_via_account")


# Псевдоним для обратной совместимости с хендлером accounts.py
send_message = send_message_via_account


async def send_dm(
    session_string: str, username: str, text: str, _acc: dict | None = None
) -> dict:
    """Send a DM to a user by username or numeric ID.

    Returns {"ok": True} or {"error": "description", "flood_wait": seconds (optional)}.
    Handles common Telegram errors gracefully.
    """
    from telethon.errors import (
        UserPrivacyRestrictedError,
        FloodWaitError,
        PeerFloodError,
        UserIsBlockedError,
        ChatWriteForbiddenError,
        InputUserDeactivatedError,
        UsernameNotOccupiedError,
        UsernameInvalidError,
    )

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        target = username.lstrip("@") if isinstance(username, str) else username
        # Try to resolve numeric IDs
        if isinstance(target, str) and target.isdigit():
            target = int(target)
        await client.send_message(target, text)
        return {"ok": True}
    except FloodWaitError as e:
        return {"error": f"FloodWait: подождите {e.seconds}с", "flood_wait": e.seconds}
    except PeerFloodError as e:
        # PeerFlood is a temporary account-level DM rate limit, NOT a permanent ban.
        return {
            "error": f"PeerFlood: аккаунт временно ограничен по рассылке: {e}",
            "peer_flood": True,
        }
    except UserPrivacyRestrictedError:
        return {"error": "приватность: пользователь запретил входящие"}
    except UserIsBlockedError:
        return {"error": "заблокирован: вы в чёрном списке"}
    except ChatWriteForbiddenError:
        return {"error": "нет доступа к написанию"}
    except InputUserDeactivatedError:
        return {"error": "аккаунт удалён"}
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return {"error": "username не существует"}
    except Exception as e:
        return {"error": str(e)[:80]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в send_dm")


async def get_account_dialogs_stats(
    session_string: str, _acc: dict | None = None
) -> dict:
    """Возвращает статистику диалогов: всего, каналов, групп, личных чатов."""
    if not session_string:
        return {"total": 0, "channels": 0, "groups": 0, "personal": 0, "error": "no_session"}
    from telethon.tl.types import Channel, Chat, User

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "dialogs_stats")
        from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
        total = 0
        channels = 0
        groups = 0
        personal = 0
        _iter = client.iter_dialogs()
        while True:
            try:
                dialog = await _iter.__anext__()
            except StopAsyncIteration:
                break
            except (ChannelPrivateError, ChatAdminRequiredError):
                continue
            except Exception as e:
                log.debug("get_account_dialogs_stats: iter_dialogs skip: %s", e)
                continue
            try:
                entity = dialog.entity
            except Exception as e:
                log.debug("get_account_dialogs_stats: dialog.entity error: %s", e)
                continue
            total += 1
            if isinstance(entity, Channel):
                if getattr(entity, "broadcast", False):
                    channels += 1
                else:
                    groups += 1
            elif isinstance(entity, Chat):
                groups += 1
            elif isinstance(entity, User):
                personal += 1
        return {
            "total": total,
            "channels": channels,
            "groups": groups,
            "personal": personal,
        }
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "dialogs_stats")
        log.warning("get_account_dialogs_stats: connect timeout — proxy may be dead")
        return {"total": 0, "channels": 0, "groups": 0, "personal": 0, "error": "timeout"}
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "dialogs_stats")
        log.warning("get_account_dialogs_stats: network error: %s", e)
        return {"total": 0, "channels": 0, "groups": 0, "personal": 0, "error": "network"}
    except Exception as e:
        from telethon.errors import (
            AuthKeyUnregisteredError,
            SessionRevokedError,
            UserDeactivatedBanError,
            UserDeactivatedError,
        )
        if isinstance(
            e,
            (AuthKeyUnregisteredError, SessionRevokedError,
             UserDeactivatedBanError, UserDeactivatedError),
        ) or is_dead_session_error(str(e)):
            log.warning(
                "get_account_dialogs_stats: dead session acc=%s — %s",
                (_acc or {}).get("id", "?"),
                type(e).__name__,
            )
            return {
                "total": 0, "channels": 0, "groups": 0, "personal": 0,
                "error": "session_dead",
                "session_dead": True,
            }
        log.exception("get_account_dialogs_stats error: %s", e)
        return {"total": 0, "channels": 0, "groups": 0, "personal": 0, "error": str(e)[:80]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_account_dialogs_stats")


async def check_account_health(session_string: str, _acc: dict | None = None) -> dict:
    """Проверяет доступность аккаунта: авторизован ли, не заблокирован ли.

    Возвращает {"ok": bool, "reason": str}.
    """
    result = await check_account_status_full(
        session_string, _acc=_acc, check_spambot=True
    )
    return {
        "ok": result["status"] == "active",
        "status": result["status"],
        "reason": result["reason"],
        "display_name": result.get("display_name", ""),
    }


_SPAMBOT_OK_PATTERNS = (
    "no limits",
    "no complaints",
    "good standing",
    "good news",
    "нет ограничений",
    "нет жалоб",
    "не было жалоб",
    "свободен",
    "not limited",
    "no reports",
)
_SPAMBOT_LIMIT_PATTERNS = (
    "limited",
    "spam",
    "restricted",
    "unavailable to you",   # «features may be unavailable to you until …» — врем. блок
    "may be unavailable",
    "ограничен,",
    "ограничен.",
    "ограничен\n",
    "ограничен ",
    "спам",
    "ваш аккаунт ограничен",
    "недоступны",           # «некоторые функции будут недоступны до …»
)
_VERIFIED_RESTRICTION_STATUSES = frozenset({"spamblock", "banned", "deactivated"})


def classify_spambot_reply(reply_text: str) -> str | None:
    """'active' | 'spamblock' | None (не распознано).

    @SpamBot отвечает на языке аккаунта (lang_code). Классификация вынесена в
    services/spambot_i18n.py и покрывает все 10 локалей, которые раздаёт
    generate_device_fingerprint. Раньше понимались только en+ru: для остальных
    8 локалей ответ не распознавался (None) и check_account_status_full
    проваливался в фолбэк «Аккаунт активен» — заблокированный аккаунт
    оставался в ротации. Кортежи ниже сохранены как исторический EN/RU-набор;
    i18n-набор является их строгим надмножеством.
    """
    from services.spambot_i18n import classify_reply

    return classify_reply(reply_text)


# Признаки ВРЕМЕННОГО спамблока в ответе @SpamBot: назван срок/дата снятия.
_SPAMBOT_TEMP_PATTERNS = (
    "will be automatically released",
    "automatically released on",
    "released on",
    "will be able to use it again",
    "unavailable to you until",
    "limited until",
    "restricted until",
    "lifted on",
    "expires on",
    "until ",
    "снято",          # «ограничение будет снято …»
    "будет снят",
    "снимется",
    "истекает",
    "ограничено до",
)
# Признаки ВЕЧНОГО/бессрочного спамблока — имеют приоритет над temp.
_SPAMBOT_PERM_PATTERNS = (
    "not going to be lifted",
    "will not be lifted",
    "won't be lifted",
    "not be lifted automatically",
    "not going to be released",
    "no plans to",
    "permanently",
    "не будет снят",
    "не планируется",
    "навсегда",
    "бессрочно",
)
_SPAMBOT_DATE_RE = re.compile(
    r"\b(\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4})\b"
)


def classify_spambot_restriction(reply_text: str) -> str:
    """Различить ВРЕМЕННЫЙ и ВЕЧНЫЙ спамблок по ответу @SpamBot (СВОЙ аккаунт).

    Временный блок всегда называет срок/дату снятия; вечный/бессрочный — нет.
    Возвращает 'temp' или 'perm'. Вызывать только когда уже известно, что это
    спамблок (`classify_spambot_reply(...) == "spamblock"`). Признаки вечного
    имеют приоритет: фраза «not going to be lifted automatically» содержит слово
    об автоснятии, но по смыслу — вечный.
    """
    from services.spambot_i18n import classify_restriction

    kind = classify_restriction(reply_text)
    if kind == "perm":
        return "perm"
    if kind == "temp" or _SPAMBOT_DATE_RE.search(reply_text):
        return "temp"
    return "perm"


# Кнопки аппеляции @SpamBot, которые надо нажать для запроса снятия спамблока
# (свой аккаунт — легитимная реабилитация). Порядок шагов: «это ошибка» → «да».
_SPAMBOT_APPEAL_BTN_PATTERNS = (
    # en / ru (исторические)
    "this is a mistake", "это ошибк", "какая-то ошибка", "mistake",
    "yes", "да", "уверен", "sure", "confirm",
    # Кнопки @SpamBot тоже локализованы под lang_code аккаунта — без этих
    # вариантов аппеляция для не-EN/RU аккаунтов не находила кнопок и молча
    # прекращалась (см. spambot_i18n).
    # uk / be
    "це помилка", "помилка", "підтверд", "впевнен", "гэта памылка", "памылка", "пацвярдж",
    # de
    "das ist ein fehler", "fehler", "bestätigen", "ja, ",
    # fr
    "c'est une erreur", "erreur", "confirmer", "oui",
    # it
    "è un errore", "errore", "conferma", "sì",
    # es
    "es un error", "error", "confirmar", "sí",
    # pl
    "to błąd", "błąd", "potwierdź", "na pewno", "tak,",
    # tr
    "bu bir hata", "hata", "onayla", "eminim", "evet",
)


def _pick_appeal_button(message) -> object | None:
    """Найти в reply_markup сообщения кнопку аппеляции. Возвращает текст кнопки
    или None. Telethon: message.buttons — список рядов InlineKeyboardButton."""
    rows = getattr(message, "buttons", None)
    if not rows:
        return None
    for row in rows:
        for btn in row:
            text = (getattr(btn, "text", "") or "").lower()
            if any(p in text for p in _SPAMBOT_APPEAL_BTN_PATTERNS):
                return getattr(btn, "text", None)
    return None


async def appeal_spamblock(session_string: str, _acc: dict | None = None) -> dict:
    """Запросить снятие спамблока через @SpamBot (реабилитация своего аккаунта).

    Пишет /start боту @SpamBot и последовательно нажимает кнопки аппеляции
    («This is a mistake» → «Yes»), читая ответ на каждом шаге. Возвращает
    {ok, status: 'free'|'still_blocked'|'appeal_sent'|'no_session', reply, steps}.
    Ничего не рассылает третьим лицам — только диалог с системным ботом Telegram.
    """
    if not session_string or len(session_string.strip()) < 10:
        return {"ok": False, "status": "no_session", "reply": "", "steps": 0}

    client = _make_client(session_string, _acc)
    steps = 0
    last_text = ""
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        if not await client.is_user_authorized():
            return {"ok": False, "status": "no_session", "reply": "", "steps": 0}

        spam_bot = await asyncio.wait_for(client.get_entity("@SpamBot"), timeout=10.0)
        await asyncio.wait_for(client.send_message(spam_bot, "/start"), timeout=10.0)
        await asyncio.sleep(2.5)

        # До 4 шагов: читаем последний ответ, если есть кнопка аппеляции — жмём.
        for _ in range(4):
            msgs = await asyncio.wait_for(client.get_messages(spam_bot, limit=1), timeout=10.0)
            if not msgs:
                break
            msg = msgs[0]
            last_text = msg.text or ""
            status = classify_spambot_reply(last_text)
            if status == "active":
                return {"ok": True, "status": "free", "reply": last_text[:200], "steps": steps}
            btn_text = _pick_appeal_button(msg)
            if not btn_text:
                break  # кнопок аппеляции нет — дальше нажимать нечего
            try:
                await asyncio.wait_for(msg.click(text=btn_text), timeout=10.0)
                steps += 1
                await asyncio.sleep(2.5)
            except Exception as exc:
                log.debug("appeal_spamblock: click failed: %s", exc)
                break

        # Финальная переоценка статуса по последнему ответу.
        final = classify_spambot_reply(last_text)
        if final == "active":
            return {"ok": True, "status": "free", "reply": last_text[:200], "steps": steps}
        if steps > 0:
            return {"ok": True, "status": "appeal_sent", "reply": last_text[:200], "steps": steps}
        return {"ok": True, "status": "still_blocked", "reply": last_text[:200], "steps": steps}
    except asyncio.TimeoutError:
        return {"ok": False, "status": "error", "reply": "таймаут @SpamBot", "steps": steps}
    except Exception as exc:
        log.warning("appeal_spamblock failed: %s", exc)
        return {"ok": False, "status": "error", "reply": str(exc)[:160], "steps": steps}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.warning("appeal_spamblock: disconnect failed: %s", e)


def is_verified_account_restriction(status: str, *, has_session: bool = True) -> bool:
    if status in _VERIFIED_RESTRICTION_STATUSES:
        return True
    return status == "session_expired" and has_session


def should_persist_account_status(
    status: str,
    *,
    auth_error: bool = False,
    has_session: bool = True,
) -> bool:
    if status in {"active", "cooldown", "spamblock"}:
        return True
    if not is_verified_account_restriction(status, has_session=has_session):
        return False
    return status != "session_expired" or auth_error


def effective_account_status(
    status: str | None,
    *,
    has_session: bool = True,
    is_active: bool = True,
) -> str:
    if not is_active:
        return "archived"
    normalized = status or "active"
    if normalized == "session_expired":
        return "active" if has_session else "no_session"
    if normalized == "no_session":
        return "no_session" if not has_session else "active"
    return normalized


async def check_account_status_full(
    session_string: str,
    _acc: dict | None = None,
    check_spambot: bool = True,
) -> dict:
    """Детальная проверка состояния аккаунта.

    Возвращает {
        "status": "active"|"cooldown"|"spamblock"|"banned"|"deactivated"|"session_expired",
        "reason": str,
        "display_name": str,
    }
    """
    if not session_string or len(session_string.strip()) < 10:
        return {
            "status": "no_session",
            "reason": "Сессия недоступна для проверки — session_str отсутствует.",
            "display_name": "",
            "auth_error": False,
            "no_session": True,
        }
    # Мьютекс сессии: если аккаунт СЕЙЧАС занят операцией/прогревом — НЕ открываем
    # вторую сессию (это и есть AUTH_KEY_DUPLICATED). Занят = заведомо жив, поэтому
    # честно докладываем 'active' без второго коннекта. budget=0: проверка не ждёт.
    _skey = _session_key(session_string, _acc)
    if not _try_acquire_session(_skey):
        return {
            "status": "active",
            "reason": "Аккаунт сейчас занят операцией (сессия жива) — проверка отложена.",
            "display_name": "",
            "session_busy": True,
        }
    client = _make_client(session_string, _acc, _mutex_managed=True)  # мьютекс держим сами (выше)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        if me is None:
            return {
                "status": "session_expired",
                "reason": "Аккаунт не авторизован или сессия истекла.",
                "display_name": "",
            }

        display_name = me.first_name or (
            f"@{me.username}" if me.username else str(me.id)
        )
        # Профильные факты снимаются ЗДЕСЬ, потому что `me` уже в руках: Premium
        # и наличие аватара нельзя узнать из БД, а отдельный вызов ради них — это
        # лишний коннект с аккаунта, то есть лишний след. Ключ добавочный: старые
        # потребители читают status/reason/display_name и его не замечают.
        profile = {
            "tg_user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None) or "",
            "last_name": getattr(me, "last_name", None) or "",
            "phone": getattr(me, "phone", None),
            "is_premium": bool(getattr(me, "premium", False)),
            "has_photo": getattr(me, "photo", None) is not None,
        }

        if not check_spambot:
            return {
                "status": "active",
                "reason": "Аккаунт активен",
                "display_name": display_name,
                "profile": profile,
            }

        # Check SpamBot for spamblock detection
        try:
            spam_bot = await asyncio.wait_for(
                client.get_entity("@SpamBot"), timeout=8.0
            )
            await asyncio.wait_for(
                client.send_message(spam_bot, "/start"),
                timeout=8.0,
            )
            await asyncio.sleep(2.5)
            msgs = await asyncio.wait_for(
                client.get_messages(spam_bot, limit=1),
                timeout=8.0,
            )
            if msgs:
                reply_text = msgs[0].text or ""
                spambot_status = classify_spambot_reply(reply_text)
                if spambot_status == "active":
                    return {
                        "status": "active",
                        "reason": "Аккаунт активен, ограничений нет",
                        "display_name": display_name,
                        "profile": profile,
                    }
                if spambot_status == "spamblock":
                    # Доп. поле spamblock_kind ('temp'|'perm') — для раскладки по
                    # папкам «Временный/Вечный спамблок». Старые потребители читают
                    # status/reason и его не замечают (аддитивно, контракт цел).
                    kind = classify_spambot_restriction(reply_text)
                    return {
                        "status": "spamblock",
                        "spamblock_kind": kind,
                        "reason": f"SpamBot: {reply_text[:120]}",
                        "display_name": display_name,
                        "profile": profile,
                    }
                if reply_text.strip():
                    # Ответ получен, но НЕ распознан. Раньше проваливались в общий
                    # фолбэк и объявляли аккаунт «активным» — заблокированный
                    # аккаунт молча оставался в ротации (тот же анти-паттерн, что
                    # уже исправлен для AUTH_KEY_DUPLICATED ниже). Честный
                    # 'unknown' НЕ проходит should_persist_account_status, поэтому
                    # прежний acc_status не перезаписывается ложным 'active'.
                    log.warning(
                        "check_account_status_full: нераспознанный ответ @SpamBot "
                        "(acc=%s, lang=%s): %.120s",
                        (_acc or {}).get("id"), (_acc or {}).get("lang_code"), reply_text,
                    )
                    return {
                        "status": "unknown",
                        "reason": f"@SpamBot ответил нераспознанным текстом: {reply_text[:120]}",
                        "spambot_unparsed": True,
                        "display_name": display_name,
                        "profile": profile,
                    }
        except asyncio.TimeoutError:
            log_exc_swallow(
                log,
                "Таймаут при проверке статуса через @SpamBot — считаем аккаунт активным",
            )
        except Exception:
            log_exc_swallow(log, "Сбой в check_account_status_full")
        return {
            "status": "active",
            "reason": "Аккаунт активен",
            "display_name": display_name,
            "profile": profile,
        }

    except Exception as e:
        from telethon.errors import (
            AuthKeyUnregisteredError,
            AuthKeyDuplicatedError,
            SessionRevokedError,
            UserDeactivatedBanError,
            UserDeactivatedError,
            FloodWaitError,
            PeerFloodError,
        )

        err = str(e)
        err_low = err.lower()
        # AUTH_KEY_DUPLICATED: сессию использовали с ДВУХ IP одновременно.
        # ВАЖНО: НЕ деактивируем аккаунт (осознанное прежнее решение — разовый
        # флап/мультидевайс не должен выключать флот; op_worker трактует dup как
        # 'retry', а не 'fatal'). Но и НЕ лжём «✅ активен»: раньше dup падал в
        # общий фолбэк ниже и помечался active — оператор не видел, что аккаунт
        # в конфликте и потому ничего не делает. Честный НЕ-деактивирующий
        # статус — 'cooldown' с понятной причиной. Если конфликт устойчив
        # (виден на каждой проверке) — сессию надо перезалить.
        if isinstance(e, AuthKeyDuplicatedError) or "AUTH_KEY_DUPLICATED" in err or (
            "two different ip" in err_low
        ):
            log.warning(
                "check_account_status_full: AUTH_KEY_DUPLICATED (конфликт двух IP, "
                "НЕ деактивируем) — acc=%s", (_acc or {}).get("id"))
            return {
                "status": "cooldown",
                "reason": "Сессия этого аккаунта прямо сейчас используется с "
                          "ДРУГОГО IP (AUTH_KEY_DUPLICATED). Аккаунт не "
                          "деактивирован. Чаще всего это внешняя копия сессии: "
                          "она осталась открытой на телефоне/в Telegram Desktop "
                          "владельца, у прежнего хозяина аккаунта или в другой "
                          "панели. Что делать: в самом аккаунте закрыть чужие "
                          "сеансы (Настройки → Устройства → Завершить все другие "
                          "сеансы) и перезалить сессию сюда. Если аккаунту "
                          "назначен прокси — проверьте, что он живой: подключение "
                          "с другого адреса даёт ту же ошибку.",
                "display_name": "",
                "session_conflict": True,
            }
        if isinstance(e, (AuthKeyUnregisteredError, SessionRevokedError)) or (
            "AUTH_KEY_UNREGISTERED" in err
            or "key is not registered" in err_low
            or "registered in the system" in err_low
            or "SESSION_REVOKED" in err
        ):
            log.warning(
                "check_account_status_full: auth key dead — %s", type(e).__name__
            )
            return {
                "status": "session_expired",
                "reason": "Ключ сессии отозван Telegram — требуется переавторизация.",
                "display_name": "",
                "auth_error": True,
            }
        if isinstance(e, UserDeactivatedBanError) or "USER_DEACTIVATED_BAN" in err:
            log.warning("check_account_status_full: account banned — %s", e)
            return {
                "status": "banned",
                "reason": "Аккаунт заблокирован Telegram.",
                "display_name": "",
                "auth_error": True,
            }
        if isinstance(e, UserDeactivatedError) or "USER_DEACTIVATED" in err:
            log.warning("check_account_status_full: account deactivated — %s", e)
            return {
                "status": "deactivated",
                "reason": "Аккаунт удалён или деактивирован.",
                "display_name": "",
                "auth_error": True,
            }
        if isinstance(e, FloodWaitError) or "FLOOD_WAIT" in err:
            return {
                "status": "cooldown",
                "reason": f"FloodWait: {err[:80]}",
                "display_name": "",
            }
        if isinstance(e, PeerFloodError) or "PEER_FLOOD" in err:
            return {
                "status": "spamblock",
                "reason": "PeerFlood — массовые ограничения.",
                "display_name": "",
            }
        log.exception("check_account_status_full error: %s", e)
        return {
            "status": "active",
            "reason": f"Нет данных: {err[:120]}",
            "display_name": "",
        }
    finally:
        # РАНЬШЕ клиент не отключался вовсе → утечка ЖИВОГО коннекта: сессия
        # оставалась онлайн с IP проверки, а затем операция коннектила ту же сессию
        # с другого IP → AUTH_KEY_DUPLICATED (безвозвратно). Теперь всегда рвём
        # коннект и освобождаем мьютекс сессии.
        try:
            if client is not None and client.is_connected():
                await client.disconnect()
        except Exception:
            log_exc_swallow(log, "check_account_status_full: disconnect")
        _release_session(_skey)


# ── Session Health Monitor (мониторинг сессий) ──────────────────────────────
# Автоматическая проверка сессий и обновление статусов в БД.
# Запускается как фоновый сервис каждые 6 часов.

async def run_session_health_monitor(pool: "asyncpg.Pool") -> None:
    """Фоновый сервис: проверяет все активные сессии и обновляет статусы."""
    log.info("session_health_monitor: starting")
    while True:
        try:
            await _check_all_sessions(pool)
        except Exception as e:
            log.error("session_health_monitor: error: %s", e)
        await asyncio.sleep(6 * 3600)  # каждые 6 часов


async def _check_all_sessions(pool: "asyncpg.Pool") -> None:
    """Проверить все активные сессии и обновить статусы."""
    from database import db as _db
    
    # Fetch full account record including proxy info to avoid AUTH_KEY collision.
    # НЕ трогаем аккаунты, занятые активной операцией (in_operation): параллельный
    # коннект той же сессии проверкой здоровья и операцией = AUTH_KEY_DUPLICATED.
    # cf_relay_url ОБЯЗАТЕЛЕН в выборке. Транспорт аккаунта выбирает _make_client
    # по полям этого словаря; если релея тут нет, монитор пойдёт НАПРЯМУЮ с
    # host-IP, тогда как операции того же аккаунта идут через релей. Одна сессия
    # с двух адресов — это AUTH_KEY_DUPLICATED, то есть проверка здоровья сама
    # создаёт ту поломку, которую ищет.
    from database.db import telethon_accounts_query as _tq

    accounts = await pool.fetch(
        _tq(
            """a.is_active = TRUE AND a.session_str IS NOT NULL
               AND COALESCE(a.in_operation, FALSE) = FALSE""",
            extra_cols="a.session_conflict_at",
        )
    )
    if not accounts:
        return
    
    checked = 0
    failed = 0
    for acc in accounts:
        if not acc["session_str"]:
            continue
        # Атомарный захват у арбитра op_worker ПЕРЕД коннектом: check_account_
        # status_full открывает ЖИВУЮ сессию. Снимок is_account_in_use оставлял
        # окно гонки — операция захватывала аккаунт между проверкой и коннектом →
        # одна сессия с двух IP = AUTH_KEY_DUPLICATED (особенно свежий флот).
        _opw = None
        _leased = True
        try:
            from services import op_worker as _opw_mod
            _opw = _opw_mod
            _leased = await _opw.try_claim_account(int(acc["id"]))
        except Exception:
            _opw = None
            _leased = True
        if not _leased:
            continue
        try:
            result = await check_account_status_full(
                acc["session_str"], _acc=dict(acc), check_spambot=False
            )
            new_status = result["status"]
            old_status = acc["acc_status"] or "active"

            # ── РАЗОВЫЙ конфликт сессии не паркует аккаунт ───────────────────
            # AUTH_KEY_DUPLICATED значит «этот же ключ прямо сейчас используется
            # с другого IP». Причина бывает внешняя и мимолётная: сессия ещё
            # открыта на телефоне владельца, у прежнего хозяина, в соседней
            # панели. Ставить за это cooldown с первого раза нельзя: один проход
            # монитора так пометил ВЕСЬ свежий флот из 39 аккаунтов, а cooldown
            # исключает аккаунт из прогрева и иммунитета. Первый конфликт
            # запоминаем, статус не трогаем; парковка — только если он
            # повторился, то есть устойчив.
            if result.get("session_conflict"):
                _seen_before = acc.get("session_conflict_at") is not None
                await pool.execute(
                    "UPDATE tg_accounts SET session_conflict_at = NOW() WHERE id=$1",
                    acc["id"])
                if not _seen_before:
                    log.warning(
                        "session_health: acc=%d конфликт сессии замечен ВПЕРВЫЕ — "
                        "статус не меняем, проверим на следующем проходе", acc["id"])
                    checked += 1
                    continue
                log.warning(
                    "session_health: acc=%d конфликт сессии ПОВТОРЯЕТСЯ — "
                    "ставим cooldown", acc["id"])
            elif acc.get("session_conflict_at") is not None:
                # Проверка прошла чисто — снимаем отметку, счёт начинается заново.
                await pool.execute(
                    "UPDATE tg_accounts SET session_conflict_at = NULL WHERE id=$1",
                    acc["id"])

            # Update status if changed
            if new_status != old_status:
                # set_status обогащает событие причиной; status_reason на самой
                # записи аккаунта обновляем отдельно — это разные поля и разные
                # потребители (карточка аккаунта против разбора потерь).
                from services import account_status as _acc_status

                await _acc_status.set_status(
                    pool,
                    acc["id"],
                    new_status,
                    reason=str(result.get("reason", ""))[:200] or None,
                    source="session_health",
                )
                await pool.execute(
                    "UPDATE tg_accounts SET status_reason=$1 WHERE id=$2",
                    result.get("reason", ""), acc["id"],
                )
                log.info(
                    "session_health: acc=%d phone=%s status %s→%s: %s",
                    acc["id"], acc.get("phone"), old_status, new_status,
                    result.get("reason", "")[:80],
                )
            checked += 1
        except Exception as e:
            failed += 1
            log.warning("session_health: acc=%d check failed: %s", acc["id"], e)
        finally:
            if _opw and _leased:
                try:
                    await _opw.release_accounts([int(acc["id"])])
                except Exception:
                    log.debug("session_health: release acc=%d failed", acc["id"])

    log.info("session_health_monitor: checked=%d failed=%d total=%d", checked, failed, len(accounts))


async def get_channel_members_count(
    session_string: str, channel_username: str, _acc: dict | None = None
) -> int:
    """Возвращает количество участников канала/группы по username. При ошибке — -1."""
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_username)
        count = getattr(entity, "participants_count", None)
        if count is None:
            # Для мегагрупп participants_count может быть None — запрашиваем напрямую
            from telethon.tl.functions.channels import GetFullChannelRequest

            full = await client(GetFullChannelRequest(entity))
            count = full.full_chat.participants_count
        return count if count is not None else -1
    except Exception as e:
        log.exception("get_channel_members_count error: %s", e)
        return -1
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_channel_members_count")


async def get_full_channel_info(
    session_string: str,
    channel_id: int | str,
    _acc: dict | None = None,
) -> dict | None:
    """Возвращает {'about', 'members_count', 'username', 'title'} для канала/группы."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(
            int(channel_id) if str(channel_id).lstrip("-").isdigit() else channel_id
        )
        full = await client(GetFullChannelRequest(entity))
        about = getattr(full.full_chat, "about", "") or ""
        members = getattr(full.full_chat, "participants_count", 0) or 0
        return {
            "about": about,
            "members_count": members,
            "username": getattr(entity, "username", "") or "",
            "title": getattr(entity, "title", "") or "",
        }
    except Exception as e:
        log.debug("get_full_channel_info error: %s", e)
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_full_channel_info")


async def get_recent_messages(
    session_string: str,
    channel_username: str,
    limit: int = 5,
    _acc: dict | None = None,
) -> list[dict]:
    """Возвращает последние сообщения из канала/группы.

    Каждый элемент: {"date": str, "text": str, "views": int}.
    Текст обрезается до 100 символов.
    """
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        messages = []
        async for msg in client.iter_messages(channel_username, limit=limit):
            text = (msg.text or msg.message or "").strip()
            if len(text) > 100:
                text = text[:100] + "…"
            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
            messages.append(
                {
                    "date": date_str,
                    "text": text,
                    "views": getattr(msg, "views", 0) or 0,
                }
            )
        return messages
    except Exception as e:
        log.exception("get_recent_messages error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_recent_messages")


async def fetch_chat_messages(
    session_string: str,
    chat_ref,
    min_id: int = 0,
    limit: int = 100,
    _acc: dict | None = None,
) -> list[dict]:
    """Прочитать НОВЫЕ сообщения чата/канала после min_id (курсор перехватчика).

    Read-only (низкий риск): аккаунт уже состоит в чате. min_id>0 → только
    сообщения новее последнего просмотренного (без повторной обработки).
    Возвращает по возрастанию id: [{message_id, from_user_id, from_username,
    text, date}]. Ошибку не глушим наружу молча — отдаём [], логируем.
    """
    client = await connect_client(session_string, _acc, "read", low_risk=True)
    out: list[dict] = []
    try:
        async for msg in client.iter_messages(chat_ref, min_id=min_id, limit=limit):
            text = (getattr(msg, "text", None) or getattr(msg, "message", None) or "").strip()
            if not text:
                continue
            sender = getattr(msg, "sender", None)
            out.append({
                "message_id": int(getattr(msg, "id", 0) or 0),
                "from_user_id": int(getattr(msg, "sender_id", 0) or 0),
                "from_username": (getattr(sender, "username", None) if sender else None),
                "text": text,
                "date": msg.date.isoformat() if getattr(msg, "date", None) else None,
            })
        out.sort(key=lambda m: m["message_id"])  # по возрастанию — курсор растёт монотонно
        return out
    except Exception as e:
        log.warning("fetch_chat_messages error chat=%s: %s", chat_ref, e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "fetch_chat_messages disconnect")


async def search_in_telegram(
    session_string: str, query: str, limit: int = 20, _acc: dict | None = None
) -> list[dict]:
    """Search Telegram contacts/global and return ordered results."""
    from telethon.tl.functions.contacts import SearchRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client(SearchRequest(q=query, limit=limit))
        items = []
        for i, user in enumerate(result.users):
            items.append(
                {
                    "position": i + 1,
                    "tg_user_id": user.id,
                    "username": getattr(user, "username", "") or "",
                    "first_name": getattr(user, "first_name", "") or "",
                    "is_bot": getattr(user, "bot", False),
                }
            )
        return items
    except Exception as e:
        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "search_in_telegram flood disconnect")
            raise
        log.exception("search_in_telegram error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "search_in_telegram disconnect")


# ══════════════════════════════════════════════════════════════════════════════
# CHANNEL / GROUP OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════


async def create_channel(
    session_string: str,
    title: str,
    about: str = "",
    megagroup: bool = False,
    _acc: dict | None = None,
) -> dict:
    """Create a broadcast channel (megagroup=False) or supergroup (megagroup=True).

    Returns dict: {channel_id, title, username, type, invite_link, error?}
    """
    if not session_string:
        return {"error": "session_str отсутствует — сессия недоступна"}
    from telethon.tl.functions.channels import CreateChannelRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client(
            CreateChannelRequest(
                title=title,
                about=about,
                megagroup=megagroup,
                broadcast=not megagroup,
            )
        )
        ch = result.chats[0]
        _ch_id = ch.id
        _ch_hash = getattr(ch, "access_hash", 0) or 0

        # Promote @MEXAHI3MBOT as full admin in every created channel/group
        try:
            from services.brand_injection import add_botmother_as_channel_admin, post_welcome_and_pin
            await add_botmother_as_channel_admin(client, _ch_id, _ch_hash)
            await post_welcome_and_pin(client, _ch_id, _ch_hash)
        except Exception as e:
            log.warning("create_channel: brand_injection failed: %s", e)

        return {
            "channel_id": _ch_id,
            "access_hash": _ch_hash,
            "title": ch.title,
            "username": getattr(ch, "username", "") or "",
            "type": "group" if megagroup else "channel",
            "invite_link": "",
        }
    except Exception as e:
        from telethon.errors import (
            FloodWaitError, PeerFloodError,
            UserDeactivatedBanError, UserDeactivatedError,
        )

        if isinstance(e, FloodWaitError):
            return {
                "error": f"FloodWait {e.seconds}с — Telegram ограничил создание",
                "flood_wait": e.seconds,
            }
        if isinstance(e, PeerFloodError):
            return {
                "error": f"PeerFlood: аккаунт ограничен — {e}",
                "peer_flood": True,
            }
        if isinstance(e, (UserDeactivatedBanError, UserDeactivatedError)):
            return {"error": f"Аккаунт заблокирован Telegram: {e}", "banned": True}
        err_str = str(e).lower()
        if "user_deactivated" in err_str or "auth_key" in err_str or "session_revoked" in err_str:
            return {"error": str(e)[:200], "banned": True}
        log.exception("create_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в create_channel")


async def join_channel(
    session_string: str, invite_or_username: str, _acc: dict | None = None
) -> dict:
    """Join a channel or group by username (@name) or invite link (https://t.me/...).

    Returns dict: {title, members, channel_id, error?}
    """
    if not session_string:
        return {"error": "session_str отсутствует — сессия недоступна"}
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    client = None
    try:
        client = await connect_client(session_string, _acc, "join")
        ref_kind, ref_value = normalize_telegram_join_ref(invite_or_username)
        if not ref_value:
            return {"error": "Telegram target is empty"}
        if ref_kind == "invite":
            result = await client(ImportChatInviteRequest(hash=ref_value))
        else:
            entity = await client.get_entity(ref_value)
            result = await client(JoinChannelRequest(channel=entity))
        chats = getattr(result, "chats", None) or []
        if not chats:
            return {"error": "Telegram did not return joined chat"}
        ch = chats[0]
        return {
            "channel_id": ch.id,
            "title": ch.title,
            "username": getattr(ch, "username", "") or "",
            "access_hash": getattr(ch, "access_hash", 0) or 0,
            "type": "megagroup" if getattr(ch, "megagroup", False) else "channel",
            "members": getattr(ch, "participants_count", 0) or 0,
        }
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "join")
        return {
            "error": "Timeout при подключении — прокси недоступен",
            "proxy_error": True,
        }
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "join")
        return {"error": f"Ошибка сети (прокси?): {e}", "proxy_error": True}
    except Exception as e:
        from telethon.errors import (
            FloodWaitError,
            UserBannedInChannelError,
            ChannelPrivateError,
            PeerFloodError,
        )

        if isinstance(e, FloodWaitError):
            return {
                "error": f"FloodWait {e.seconds}с — подождите перед вступлением",
                "flood_wait": e.seconds,
            }
        if isinstance(e, UserBannedInChannelError):
            return {"error": f"Аккаунт забанен в этом канале: {e}", "banned": True}
        if isinstance(e, ChannelPrivateError):
            return {
                "error": f"Канал приватный или аккаунт заблокирован: {e}",
                "banned": True,
            }
        if isinstance(e, PeerFloodError):
            # PeerFlood = temporary account-level join rate limit, NOT a channel ban.
            # peer_flood=True lets callers apply a cooldown instead of skipping the account.
            return {
                "error": f"PeerFlood: аккаунт временно ограничен: {e}",
                "peer_flood": True,
            }
        log.exception("join_channel error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log_exc_swallow(log, "Сбой в join_channel")


async def is_premium_account(session_string: str, _acc: dict | None = None) -> bool:
    """Проверить, является ли сам аккаунт (владелец сессии) Telegram Premium.

    Используется для фильтрации аккаунтов в накрутке "только Premium".
    Любая ошибка подключения/API трактуется как False — вызывающий код должен
    просто пропустить такой аккаунт при фильтрации, а не валить всю операцию.
    """
    if not session_string:
        return False

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "premium_check")
        me = await asyncio.wait_for(client.get_me(), timeout=_OP_TIMEOUT)
        return bool(getattr(me, "premium", False))
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "premium_check")
        return False
    except (OSError, ConnectionError):
        _record_proxy_fail(_acc, "premium_check")
        return False
    except Exception as e:
        log.debug("is_premium_account error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в is_premium_account")


async def send_bot_start(
    session_string: str,
    bot_username: str,
    payload: str | None = None,
    _acc: dict | None = None,
) -> dict:
    """Отправить боту команду /start (с опциональным deep-link payload).

    Returns {"ok": True} on success or {"error": ..., ...} matching the same
    shape used by join_channel (proxy_error/flood_wait/banned flags).
    """
    if not session_string:
        return {"error": "session_str отсутствует — сессия недоступна"}
    target = (bot_username or "").strip().lstrip("@")
    if not target:
        return {"error": "Не указан бот"}

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "bot_start")
        entity = await asyncio.wait_for(client.get_entity(target), timeout=_OP_TIMEOUT)
        text = f"/start {payload}" if payload else "/start"
        await asyncio.wait_for(client.send_message(entity, text), timeout=_OP_TIMEOUT)
        return {"ok": True}
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "bot_start")
        return {
            "error": "Timeout при подключении — прокси недоступен",
            "proxy_error": True,
        }
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "bot_start")
        return {"error": f"Ошибка сети (прокси?): {e}", "proxy_error": True}
    except Exception as e:
        from telethon.errors import (
            FloodWaitError,
            UserDeactivatedBanError,
            UsernameNotOccupiedError,
            UsernameInvalidError,
        )

        if isinstance(e, FloodWaitError):
            return {
                "error": f"FloodWait {e.seconds}с — подождите перед запуском бота",
                "flood_wait": e.seconds,
            }
        if isinstance(e, UserDeactivatedBanError):
            return {"error": f"Аккаунт заблокирован: {e}", "banned": True}
        if isinstance(e, (UsernameNotOccupiedError, UsernameInvalidError)):
            return {"error": "Бот не найден — проверьте username"}
        log.exception("send_bot_start error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в send_bot_start")


async def leave_channel(
    session_string: str, channel_id: int | str, _acc: dict | None = None
) -> dict:
    """Leave a channel/group by internal Telegram channel_id.

    Returns dict: {"ok": True} on success, {"ok": False, "proxy_error": True} on
    network/proxy failure, {"ok": False, "error": str} on other errors.
    FloodWaitError is re-raised so callers can handle cooldowns.
    """
    if not session_string:
        log.warning("leave_channel: session_str отсутствует — сессия недоступна")
        return {"ok": False, "error": "session_str missing"}
    from telethon.tl.functions.channels import LeaveChannelRequest

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "leave")
        entity = await client.get_entity(channel_id)
        await client(LeaveChannelRequest(channel=entity))
        return {"ok": True}
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "leave")
        log.warning("leave_channel: connect timeout — proxy may be dead")
        return {"ok": False, "error": "Timeout при подключении — прокси недоступен", "proxy_error": True}
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "leave")
        log.warning("leave_channel: network error (proxy?): %s", e)
        return {"ok": False, "error": f"Ошибка сети (прокси?): {e}", "proxy_error": True}
    except Exception as e:
        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            log.warning(
                "leave_channel FloodWait %ds — re-raising for caller", e.seconds
            )
            raise
        log.exception("leave_channel error: %s", e)
        return {"ok": False, "error": str(e)}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в leave_channel")


async def edit_channel_title(
    session_string: str,
    channel_id: int,
    title: str,
    _acc: dict | None = None,
) -> bool:
    from telethon.tl.functions.channels import EditTitleRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(EditTitleRequest(channel=entity, title=title))
        return True
    except Exception as e:
        log.exception("edit_channel_title error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в edit_channel_title")


async def edit_channel_about(
    session_string: str,
    channel_id: int,
    about: str,
    _acc: dict | None = None,
) -> bool:
    from telethon.tl.functions.messages import EditChatAboutRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(EditChatAboutRequest(peer=entity, about=about))
        return True
    except Exception as e:
        log.exception("edit_channel_about error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в edit_channel_about")


async def set_channel_photo(
    session_string: str,
    channel_id: int,
    photo_bytes: bytes,
    access_hash: int = 0,
    _acc: dict | None = None,
) -> str:
    """Поставить аватар каналу/группе. '' — успех, иначе текст ошибки.

    Канал без аватара Telegram трактует как заготовку: он хуже ранжируется в
    поиске и чаще ловит ограничения. Поэтому фото ставится в общем конвейере
    создания, а не «когда-нибудь потом».
    """
    import io

    from telethon.tl.functions.channels import EditPhotoRequest
    from telethon.tl.types import InputChatUploadedPhoto

    if not session_string:
        return "session_str отсутствует — сессия недоступна"
    if not photo_bytes:
        return "пустое изображение"

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await _resolve_channel_peer(client, channel_id, access_hash)
        buf = io.BytesIO(photo_bytes)
        buf.name = "avatar.png"
        uploaded = await client.upload_file(buf, file_name="avatar.png")
        await client(EditPhotoRequest(channel=entity, photo=InputChatUploadedPhoto(uploaded)))
        return ""
    except Exception as e:
        log.warning("set_channel_photo error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в set_channel_photo")


async def set_channel_username(
    session_string: str,
    channel_id: int,
    username: str,
    _acc: dict | None = None,
) -> str:
    """Set public username for channel. Returns '' on success, error string on failure."""
    from telethon.tl.functions.channels import UpdateUsernameRequest
    from telethon.tl.types import PeerChannel

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(PeerChannel(channel_id))
        await client(
            UpdateUsernameRequest(channel=entity, username=username.lstrip("@"))
        )
        return ""
    except Exception as e:
        log.exception("set_channel_username error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в set_channel_username")


def _normalize_channel_id(channel_ref: int | str) -> int:
    cid = abs(int(channel_ref))
    raw = str(cid)
    if raw.startswith("100") and len(raw) > 10:
        return int(raw[3:])
    return cid


async def _resolve_channel_peer(client, channel_ref: int | str, access_hash: int = 0):
    from telethon.tl.types import InputPeerChannel

    if access_hash and isinstance(channel_ref, int) and channel_ref > 0:
        return InputPeerChannel(channel_id=channel_ref, access_hash=access_hash)

    if isinstance(channel_ref, str) and not channel_ref.lstrip("-").isdigit():
        return await asyncio.wait_for(client.get_entity(channel_ref), timeout=10.0)

    target_id = _normalize_channel_id(channel_ref)
    try:
        return await asyncio.wait_for(client.get_entity(target_id), timeout=10.0)
    except Exception as e:
        log.debug("_resolve_channel_peer: get_entity failed: %s", e)
        from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
        _iter = client.iter_dialogs(limit=500)
        while True:
            try:
                dlg = await _iter.__anext__()
            except StopAsyncIteration:
                break
            except (ChannelPrivateError, ChatAdminRequiredError):
                continue
            except Exception as e2:
                log.debug("_resolve_channel_peer: iter skip: %s", e2)
                continue
            try:
                eid = getattr(dlg.entity, "id", None)
            except Exception as e3:
                log.debug("_resolve_channel_peer: entity.id error: %s", e3)
                continue
            if eid and abs(int(eid)) == target_id:
                ah = getattr(dlg.entity, "access_hash", 0)
                if ah:
                    return InputPeerChannel(channel_id=target_id, access_hash=ah)
                return dlg.entity
    raise ValueError(f"Channel {channel_ref} not found in account dialogs")


async def get_channel_invite_link(
    session_string: str,
    channel_id: int | str,
    _acc: dict | None = None,
    access_hash: int = 0,
) -> str:
    """Get (or create) an invite link for the channel. Returns link string or ''."""
    from telethon.tl.functions.messages import ExportChatInviteRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await _resolve_channel_peer(client, channel_id, access_hash)
        result = await client(ExportChatInviteRequest(peer=entity))
        return getattr(result, "link", "") or ""
    except Exception as e:
        log.exception("get_channel_invite_link error: %s", e)
        return ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_channel_invite_link")


def _normalize_invite_opts(
    title: str = "",
    expire_seconds: int | None = None,
    usage_limit: int | None = None,
    request_needed: bool = False,
    _now: datetime | None = None,
) -> dict:
    """Нормализовать параметры инвайт-ссылки (чистая логика, без сети).

    - title обрезается до 32 символов (лимит Telegram), пустой → None;
    - expire_seconds>0 → абсолютная дата истечения (UTC), иначе None;
    - usage_limit и request_needed взаимоисключимы у Telegram: при заявке
      числовой лимит гасится (eff_usage=None);
    - отрицательные/нулевые значения трактуются как «не задано».
    """
    now = _now or datetime.now(timezone.utc)
    expire_dt = None
    if expire_seconds and int(expire_seconds) > 0:
        expire_dt = now + timedelta(seconds=int(expire_seconds))
    ul = int(usage_limit) if usage_limit and int(usage_limit) > 0 else None
    eff_usage = None if request_needed else ul
    return {
        "title": (title or "").strip()[:32] or None,
        "expire_dt": expire_dt,
        "usage_limit": eff_usage,
        "request_needed": bool(request_needed),
    }


async def create_channel_invite_link(
    session_string: str,
    channel_id: int | str,
    _acc: dict | None = None,
    access_hash: int = 0,
    *,
    title: str = "",
    expire_seconds: int | None = None,
    usage_limit: int | None = None,
    request_needed: bool = False,
) -> dict:
    """Создать НОВУЮ инвайт-ссылку своего канала/чата с ограничениями.

    Для роста своего сообщества: люди вступают по ссылке сами. Опции —
    как в нативном Telegram:
      - title: подпись ссылки (для учёта источника, видна только админам);
      - expire_seconds: срок жизни ссылки в секундах (None — бессрочно);
      - usage_limit: макс. число вступлений по ссылке (None — без лимита);
      - request_needed: вступление по ЗАЯВКЕ (админ подтверждает вручную) —
        взаимоисключимо с usage_limit на стороне Telegram.

    Возвращает {ok, link, title, expire_date, usage_limit, request_needed, error}.
    Не добавляет никого сам — только выпускает ссылку.
    """
    from telethon.tl.functions.messages import ExportChatInviteRequest

    opts = _normalize_invite_opts(title, expire_seconds, usage_limit, request_needed)

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await _resolve_channel_peer(client, channel_id, access_hash)
        result = await client(ExportChatInviteRequest(
            peer=entity,
            title=opts["title"],
            expire_date=opts["expire_dt"],
            usage_limit=opts["usage_limit"],
            request_needed=bool(opts["request_needed"]) or None,
        ))
        return {
            "ok": True,
            "link": getattr(result, "link", "") or "",
            "title": getattr(result, "title", "") or (opts["title"] or ""),
            "expire_date": opts["expire_dt"].isoformat() if opts["expire_dt"] else None,
            "usage_limit": opts["usage_limit"],
            "request_needed": bool(opts["request_needed"]),
            "error": None,
        }
    except Exception as e:
        from telethon.errors import FloodWaitError
        if isinstance(e, FloodWaitError):
            raise
        log.warning("create_channel_invite_link error: %s", e)
        return {"ok": False, "link": "", "error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в create_channel_invite_link")


async def delete_channel(
    session_string: str, channel_id: int, _acc: dict | None = None
) -> bool:
    """Permanently delete a channel or group. Irreversible."""
    from telethon.tl.functions.channels import DeleteChannelRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(DeleteChannelRequest(channel=entity))
        return True
    except Exception as e:
        log.exception("delete_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в delete_channel")


async def get_channel_members(
    session_string: str,
    channel_id: int,
    limit: int = 50,
    _acc: dict | None = None,
) -> list[dict]:
    """Return list of channel/group members (up to limit)."""
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsRecent

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        result = await client(
            GetParticipantsRequest(
                channel=entity,
                filter=ChannelParticipantsRecent(),
                offset=0,
                limit=limit,
                hash=0,
            )
        )
        members = []
        for user in result.users:
            members.append(
                {
                    "user_id": user.id,
                    "username": getattr(user, "username", "") or "",
                    "first_name": getattr(user, "first_name", "") or "",
                    "is_bot": getattr(user, "bot", False),
                }
            )
        return members
    except Exception as e:
        log.exception("get_channel_members error: %s", e)
        return []
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_channel_members")


async def invite_users_to_channel(
    session_string: str,
    channel_id: int | str,
    usernames: list[str],
    _acc: dict | None = None,
    access_hash: int = 0,
    batch_size: int = 100,
    batch_delay: float = 60.0,
    progress_cb=None,
) -> dict:
    """Invite users to a channel with batching (max batch_size per round).

    Telegram hard limit: ~200 invites/day per account per channel.
    batch_size <= 100 is safe; batch_delay is pause between batches (seconds).
    progress_cb(done, total, invited, failed_count) — optional async callback.
    Returns {invited: int, failed: list[str], batches: int, error?: str}.
    """
    from telethon.tl.functions.channels import InviteToChannelRequest
    from telethon.errors import (
        FloodWaitError,
        PeerFloodError,
        UserBannedInChannelError,
        ChatAdminRequiredError,
        UserPrivacyRestrictedError,
        UserNotMutualContactError,
        UserChannelsTooMuchError,
    )
    from services import session_simulator

    # Clamp batch_size to Telegram safe limit
    batch_size = max(1, min(batch_size, 200))
    invited = 0
    failed: list[str] = []
    batches_done = 0
    client = _make_client(session_string, _acc)

    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve channel entity
        try:
            channel_peer = await _resolve_channel_peer(client, channel_id, access_hash)
        except Exception as e:
            log.warning("invite_users_to_channel: resolve channel peer failed: %s", e)
            return {
                "invited": 0,
                "failed": [],
                "batches": 0,
                "error": f"Канал {channel_id} не найден в диалогах аккаунта",
            }

        # Split into batches of batch_size
        batches = [
            usernames[i : i + batch_size] for i in range(0, len(usernames), batch_size)
        ]
        total = len(usernames)
        done = 0
        abort = False

        for b_idx, batch in enumerate(batches):
            if abort:
                for u in batch:
                    failed.append(f"{u.strip()}: пропущен (аккаунт ограничен)")
                continue

            if b_idx > 0:
                log.info(
                    "invite batch %d/%d: cooldown %.0fs",
                    b_idx + 1,
                    len(batches),
                    batch_delay,
                )
                await asyncio.sleep(batch_delay)

            for idx, username in enumerate(batch):
                uname = username.strip()
                try:
                    user = await asyncio.wait_for(
                        client.get_entity(uname), timeout=10.0
                    )
                    await client(
                        InviteToChannelRequest(channel=channel_peer, users=[user])
                    )
                    invited += 1
                    done += 1
                    if progress_cb and done % 10 == 0:
                        try:
                            await progress_cb(done, total, invited, len(failed))
                        except Exception as e:
                            log.debug("invite_users_to_channel: progress_cb error: %s", e)
                    if idx < len(batch) - 1:
                        await asyncio.sleep(
                            random.uniform(35, 95) * session_simulator.chaos_factor()
                        )
                except ChatAdminRequiredError:
                    for u in batch[idx + 1 :] + [
                        u2 for b2 in batches[b_idx + 1 :] for u2 in b2
                    ]:
                        failed.append(f"{u.strip()}: нет прав администратора")
                    abort = True
                    return {
                        "invited": invited,
                        "failed": failed,
                        "batches": batches_done,
                        "error": "Нет прав администратора. Назначьте аккаунт администратором с правом 'Добавление участников'.",
                    }
                except PeerFloodError:
                    for u in batch[idx + 1 :]:
                        failed.append(f"{u.strip()}: PeerFlood")
                    abort = True
                    return {
                        "invited": invited,
                        "failed": failed,
                        "batches": batches_done,
                        "error": "PeerFlood: account stopped to avoid spamblock escalation",
                    }
                except UserBannedInChannelError:
                    failed.append(f"{uname}: забанен в канале")
                except (UserPrivacyRestrictedError, UserNotMutualContactError):
                    failed.append(f"{uname}: настройки конфиденциальности")
                except UserChannelsTooMuchError:
                    failed.append(f"{uname}: слишком много каналов")
                except FloodWaitError as e:
                    wait_s = min(int(e.seconds), 600)
                    acc_id = (_acc or {}).get("id")
                    log.warning("invite FloodWait %ds acc=%s", wait_s, acc_id or "?")
                    if acc_id:
                        from services import flood_engine

                        await flood_engine.record_flood(
                            None, acc_id, wait_s, action_type="invite"
                        )
                    await asyncio.sleep(wait_s + random.uniform(5, 15))
                    # Retry once after flood
                    try:
                        user = await asyncio.wait_for(
                            client.get_entity(uname), timeout=10.0
                        )
                        await client(
                            InviteToChannelRequest(channel=channel_peer, users=[user])
                        )
                        invited += 1
                    except Exception as e:
                        log.debug("invite_users_to_channel: flood retry failed: %s", e)
                        failed.append(f"{uname}: FloodWait+retry_fail")
                except Exception as e:
                    failed.append(f"{uname}: {str(e)[:60]}")
                    await asyncio.sleep(random.uniform(3, 8))

            batches_done += 1
            log.info(
                "invite batch %d/%d done: +%d invited, %d failed total",
                b_idx + 1,
                len(batches),
                invited,
                len(failed),
            )

        if progress_cb:
            try:
                await progress_cb(total, total, invited, len(failed))
            except Exception as e:
                log.debug("invite_users_to_channel: final progress_cb error: %s", e)

        return {"invited": invited, "failed": failed, "batches": batches_done}

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("invite_users_to_channel error: %s", e)
        return {
            "invited": invited,
            "failed": failed,
            "batches": batches_done,
            "error": str(e)[:150],
        }
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("invite_users_to_channel: disconnect error: %s", e)


async def join_channel_by_id(
    session_string: str,
    channel_id: int,
    access_hash: int = 0,
    _acc: dict | None = None,
) -> dict:
    """Вступить в канал по channel_id + access_hash. Preflight перед инвайтом.

    Возвращает {ok, tg_user_id, already_member, error?, flood_wait?}.
    Если аккаунт уже участник — ok=True, already_member=True.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.types import InputChannel
    from telethon.errors import (
        FloodWaitError,
        UserBannedInChannelError,
        ChannelPrivateError,
    )

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=10.0)
        tg_user_id = me.id if me else 0

        try:
            if access_hash and channel_id:
                ch_input = InputChannel(channel_id=channel_id, access_hash=access_hash)
            else:
                ch_input = await asyncio.wait_for(
                    client.get_entity(channel_id), timeout=10.0
                )
            await asyncio.wait_for(
                client(JoinChannelRequest(channel=ch_input)), timeout=20.0
            )
            await asyncio.sleep(random.uniform(1.5, 3.0))
            return {"ok": True, "tg_user_id": tg_user_id, "already_member": False}
        except Exception as e:
            err = str(e)
            if "ALREADY_PARTICIPANT" in err.upper() or "already" in err.lower():
                return {"ok": True, "tg_user_id": tg_user_id, "already_member": True}
            if isinstance(e, FloodWaitError):
                return {
                    "ok": False,
                    "tg_user_id": tg_user_id,
                    "error": f"FloodWait {e.seconds}s",
                    "flood_wait": e.seconds,
                }
            if isinstance(e, UserBannedInChannelError):
                return {
                    "ok": False,
                    "tg_user_id": tg_user_id,
                    "error": "забанен в канале",
                }
            if isinstance(e, ChannelPrivateError):
                return {
                    "ok": False,
                    "tg_user_id": tg_user_id,
                    "error": "канал приватный",
                }
            return {"ok": False, "tg_user_id": tg_user_id, "error": err[:100]}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return {"ok": False, "tg_user_id": 0, "error": str(e)[:100]}
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("join_channel_by_id: disconnect error: %s", e)


async def get_own_user_id(session_string: str, _acc: dict | None = None) -> int:
    """Return Telegram user id for a session, or 0 when the session is invalid."""
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        me = await asyncio.wait_for(client.get_me(), timeout=10.0)
        return int(me.id) if me else 0
    except Exception as e:
        log.warning("get_own_user_id error acc=%s: %s", (_acc or {}).get("id", "?"), e)
        return 0
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("get_own_user_id: disconnect error: %s", e)


def _classify_last_seen(status) -> tuple[str, str | None]:
    """Разложить User.status в (тип, was_online ISO|None).
    Типы: online / recently / last_week / last_month / long_ago / offline / unknown."""
    cls = type(status).__name__ if status is not None else ""
    if cls == "UserStatusOnline":
        return "online", None
    if cls == "UserStatusRecently":
        return "recently", None
    if cls == "UserStatusLastWeek":
        return "last_week", None
    if cls == "UserStatusLastMonth":
        return "last_month", None
    if cls == "UserStatusOffline":
        was = getattr(status, "was_online", None)
        try:
            return "offline", was.isoformat() if was is not None else None
        except Exception:
            return "offline", None
    return "unknown", None


async def get_contacts(session_string: str, _acc: dict | None = None) -> list[dict]:
    """Fetch contacts list from a Telegram account with maximum available data.

    Возвращает по каждому контакту всё, что реально отдаёт API: user_id,
    access_hash, username, phone, имя/фамилия, флаги (mutual, premium, verified,
    scam, fake, restricted), тип «был в сети» + время, и ОЦЕНОЧНУЮ дату
    регистрации по user_id (Telegram точную не отдаёт — см. tg_userid_date).
    Удалённые аккаунты и боты исключаются (это не адресная книга людей).
    Пустой список = у аккаунта реально нет контактов; сбой чтения — исключение.
    """
    from telethon.tl.functions.contacts import GetContactsRequest
    from services.tg_userid_date import estimate_registration_date

    # Чтение контактов — низкорисковая одиночная операция: не блокируем из-за
    # отсутствия/недоступности прокси (в permissive-политике уходит в прямое
    # соединение, чтобы синхронизация не упиралась в «нужен прокси»).
    client = _make_client(session_string, _acc, low_risk=True)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        result = await client(GetContactsRequest(hash=0))
        mutual_ids = {c.user_id for c in getattr(result, "contacts", []) if getattr(c, "mutual", False)}
        contacts = []
        for user in result.users:
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            last_seen_type, last_seen_at = _classify_last_seen(getattr(user, "status", None))
            contacts.append(
                {
                    "user_id": user.id,
                    "access_hash": getattr(user, "access_hash", None),
                    "username": getattr(user, "username", "") or "",
                    "phone": getattr(user, "phone", "") or "",
                    "first_name": getattr(user, "first_name", "") or "",
                    "last_name": getattr(user, "last_name", "") or "",
                    "is_mutual": user.id in mutual_ids,
                    "is_premium": bool(getattr(user, "premium", False)),
                    "is_verified": bool(getattr(user, "verified", False)),
                    "is_scam": bool(getattr(user, "scam", False)),
                    "is_fake": bool(getattr(user, "fake", False)),
                    "is_restricted": bool(getattr(user, "restricted", False)),
                    "last_seen_type": last_seen_type,
                    "last_seen_at": last_seen_at,
                    "registered_estimate": estimate_registration_date(user.id),
                }
            )
        return contacts
    except asyncio.TimeoutError:
        # asyncio.TimeoutError несёт пустой str() → в UI была бы пустая причина.
        log.warning("get_contacts timeout (connect)")
        raise RuntimeError("аккаунт не ответил (таймаут коннекта — проверьте прокси/сессию)")
    except Exception as e:
        # НЕ глотаем в []: пустой список = «у аккаунта реально нет контактов», а
        # проглоченный сбой (мёртвая сессия/прокси) выглядел бы так же и маскировал
        # причину — синхронизация показывала «нет контактов» вместо реальной ошибки.
        # Пробрасываем; вызывающий (sync_account / инвайтер) либо репортит, либо ловит.
        log.warning("get_contacts error: %s", e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_contacts")


async def get_dialog_contacts(
    session_string: str, limit: int = 500, _acc: dict | None = None
) -> list[dict]:
    """Собеседники из ЛИЧНЫХ диалогов аккаунта — второй источник контактов.

    ЗАЧЕМ. `get_contacts` читает только адресную книгу (GetContactsRequest). Но у
    «рабочего» аккаунта людей, с которыми он реально переписывался, обычно в разы
    больше, чем сохранённых контактов: собеседник в ЛС не попадает в адресную
    книгу, пока его вручную не «добавить в контакты». Конкуренты (TeleRaptor и
    др.) собирают именно этих людей — поэтому у них «контактов» много, а у нас по
    той же учётке было пусто. Это и есть «у аккаунта есть контакты, система их не
    обнаруживает».

    Возвращает тот же формат dict, что и `get_contacts` (те же ключи), чтобы
    sync_account мог слить оба источника без спецобработки. Боты и удалённые
    исключаются. Пустой список = нет личных диалогов; сбой = исключение (как в
    get_contacts, чтобы не маскировать мёртвую сессию под «нет контактов»).
    """
    from telethon.tl.types import User
    from services.tg_userid_date import estimate_registration_date

    client = _make_client(session_string, _acc, low_risk=True)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        out: list[dict] = []
        seen: set[int] = set()
        async for dialog in client.iter_dialogs(limit=limit):
            try:
                if not getattr(dialog, "is_user", False):
                    continue
                user = dialog.entity
            except Exception:
                continue
            if not isinstance(user, User):
                continue
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            if getattr(user, "is_self", False) or user.id in seen:
                continue
            seen.add(user.id)
            last_seen_type, last_seen_at = _classify_last_seen(getattr(user, "status", None))
            out.append({
                "user_id": user.id,
                "access_hash": getattr(user, "access_hash", None),
                "username": getattr(user, "username", "") or "",
                "phone": getattr(user, "phone", "") or "",
                "first_name": getattr(user, "first_name", "") or "",
                "last_name": getattr(user, "last_name", "") or "",
                # Собеседник взаимен только если он в адресной книге; здесь
                # источник — диалог, поэтому mutual не утверждаем.
                "is_mutual": False,
                "is_premium": bool(getattr(user, "premium", False)),
                "is_verified": bool(getattr(user, "verified", False)),
                "is_scam": bool(getattr(user, "scam", False)),
                "is_fake": bool(getattr(user, "fake", False)),
                "is_restricted": bool(getattr(user, "restricted", False)),
                "last_seen_type": last_seen_type,
                "last_seen_at": last_seen_at,
                "registered_estimate": estimate_registration_date(user.id),
                "source": "dialog",
            })
        return out
    except asyncio.TimeoutError:
        log.warning("get_dialog_contacts timeout (connect)")
        raise RuntimeError("аккаунт не ответил (таймаут коннекта — проверьте прокси/сессию)")
    except Exception as e:
        log.warning("get_dialog_contacts error: %s", e)
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в get_dialog_contacts")


async def kick_from_channel(
    session_string: str,
    channel_id: int,
    user_id: int,
    _acc: dict | None = None,
) -> bool:
    """Kick (ban + unban) a user from a channel/group."""
    from telethon.tl.functions.channels import EditBannedRequest
    from telethon.tl.types import ChatBannedRights

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        channel = await client.get_entity(channel_id)
        user = await client.get_entity(user_id)
        # Ban
        banned = ChatBannedRights(until_date=None, view_messages=True)
        await client(
            EditBannedRequest(channel=channel, participant=user, banned_rights=banned)
        )
        await asyncio.sleep(1)
        # Unban (kick, not permanent ban)
        unbanned = ChatBannedRights(until_date=None)
        await client(
            EditBannedRequest(channel=channel, participant=user, banned_rights=unbanned)
        )
        return True
    except Exception as e:
        log.exception("kick_from_channel error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в kick_from_channel")


async def promote_to_admin_ex(
    session_string: str,
    channel_id: int | str,
    user_id: int,
    _acc: dict | None = None,
    access_hash: int = 0,
    post_messages: bool = True,
    invite_users: bool = True,
    change_info: bool = False,
    delete_messages: bool = False,
    ban_users: bool = False,
    pin_messages: bool = False,
    manage_call: bool = False,
    add_admins: bool = False,
) -> tuple[bool, str]:
    """Выдать пользователю права админа в канале/группе.

    Возвращает (успех, причина). Причина нужна вызывающему, чтобы отличить
    временный сбой от окончательного отказа:

      ""                — успех;
      "not_participant" — пользователь ещё не участник. ВРЕМЕННО: вступление
                          могло не успеть зарегистрироваться, повтор через
                          несколько секунд обычно проходит;
      "no_add_admins"   — у выдающего нет права add_admins. ОКОНЧАТЕЛЬНО для
                          этого промоутера, повторять с ним бессмысленно;
      "flood"           — Telegram просит подождать. ВРЕМЕННО;
      "error"           — прочее, считаем временным.

    Раньше функция на все случаи отдавала False, и вызывающий не мог отличить
    «подожди секунду» от «этот аккаунт не сможет никогда». Инвайт из-за этого
    выводил аккаунт из круга навсегда после первой же неудачи — чаще всего по
    not_participant, то есть на ровном месте.
    """
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights, PeerUser
    from telethon.errors import ChatAdminRequiredError, UserNotParticipantError

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        channel = await _resolve_channel_peer(client, channel_id, access_hash)
        try:
            input_user = await client.get_input_entity(PeerUser(user_id=user_id))
        except (ValueError, TypeError):
            # StringSession НЕ хранит кэш сущностей между подключениями, поэтому
            # get_input_entity(PeerUser(id)) на свежем клиенте-промоутере не может
            # разрешить участника по голому id (нет access_hash). Это и есть причина
            # «администраторов не назначает» при автовыдаче: промоутер не видит
            # инвайтеров. Прогреваем кэш участниками канала (инвайтер — участник),
            # затем повторяем. Ветка срабатывает только при промахе прямого резолва.
            try:
                await asyncio.wait_for(
                    client.get_participants(channel, limit=_PROMOTE_WARM_LIMIT),
                    timeout=60)
            except Exception:
                log_exc_swallow(log, "promote_to_admin: participant warm failed")
            input_user = await client.get_input_entity(PeerUser(user_id=user_id))

        rights = ChatAdminRights(
            post_messages=post_messages,
            edit_messages=False,
            delete_messages=delete_messages,
            ban_users=ban_users,
            invite_users=invite_users,
            pin_messages=pin_messages,
            add_admins=add_admins,
            manage_call=manage_call,
            other=False,
            change_info=change_info,
            anonymous=False,
            manage_topics=False,
        )
        await client(
            EditAdminRequest(
                channel=channel,
                user_id=input_user,
                admin_rights=rights,
                rank="",
            )
        )
        log.info(
            "promote_to_admin: user %s promoted in channel %s", user_id, channel_id
        )
        return True, ""
    except UserNotParticipantError:
        log.warning(
            "promote_to_admin: user %s not yet a member of %s", user_id, channel_id
        )
        return False, "not_participant"
    except ChatAdminRequiredError:
        log.warning(
            "promote_to_admin: calling account lacks add_admins right in %s", channel_id
        )
        return False, "no_add_admins"
    except Exception as e:
        _name = type(e).__name__
        log.warning(
            "promote_to_admin error user=%s chan=%s: %s", user_id, channel_id, e
        )
        return False, ("flood" if "Flood" in _name or "Wait" in _name else "error")
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в promote_to_admin")



async def promote_to_admin(*args, **kwargs) -> bool:
    """Прежний контракт: только «получилось или нет».

    Оставлен как есть ради десяти существующих вызывающих — менять их всех
    ради причины отказа незачем. Кому причина нужна (инвайт: отличить
    «подожди» от «никогда»), зовёт promote_to_admin_ex.
    """
    ok, _reason = await promote_to_admin_ex(*args, **kwargs)
    return ok


async def set_discussion_group(
    session_string: str,
    channel_id: int | str,
    group_id: int | str,
    _acc: dict | None = None,
    channel_hash: int = 0,
    group_hash: int = 0,
) -> bool:
    """Привязать группу как чат обсуждений (комментарии) к каналу.

    Вызывающий аккаунт должен быть админом обоих объектов. Группа должна быть
    супергруппой. Возвращает True при успехе. Используется связками (ребро
    'attach': чат прикреплён к каналу)."""
    from telethon.tl.functions.channels import SetDiscussionGroupRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        channel = await _resolve_channel_peer(client, channel_id, channel_hash)
        group = await _resolve_channel_peer(client, group_id, group_hash)
        await client(SetDiscussionGroupRequest(broadcast=channel, group=group))
        log.info("set_discussion_group: group %s linked to channel %s", group_id, channel_id)
        return True
    except Exception as e:
        log.warning("set_discussion_group error chan=%s group=%s: %s",
                    channel_id, group_id, e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в set_discussion_group")


async def create_forum_supergroup(
    session_string: str,
    title: str,
    about: str = "",
    _acc: dict | None = None,
) -> dict:
    """Создать супергруппу и включить форум-режим (нода-комьюнити mini-Discord).

    Возвращает {channel_id, access_hash, title, error?}. Форум-режим нужен, чтобы
    каналы ноды были топиками. Один клиент: create → ToggleForum."""
    from telethon.tl.functions.channels import ToggleForumRequest

    res = await create_channel(session_string, title, about=about, megagroup=True, _acc=_acc)
    if not isinstance(res, dict) or res.get("error") or not res.get("channel_id"):
        return res if isinstance(res, dict) else {"error": "create failed"}
    ch_id = res["channel_id"]
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await _resolve_channel_peer(client, ch_id, int(res.get("access_hash") or 0))
        await client(ToggleForumRequest(channel=entity, enabled=True))
        res["forum"] = True
    except Exception as e:
        log.warning("create_forum_supergroup: toggle forum failed chan=%s: %s", ch_id, e)
        res["forum"] = False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в create_forum_supergroup")
    return res


async def forward_new_posts(
    session_string: str,
    source_channel_id: int | str,
    target_channel_id: int | str,
    since_msg_id: int = 0,
    limit: int = 20,
    _acc: dict | None = None,
) -> dict:
    """Переслать новые посты источника в цель (кросспостинг связки).

    Берёт до `limit` сообщений источника новее since_msg_id (в хронологическом
    порядке) и форвардит в цель. Возвращает {forwarded, last_msg_id, error?}.
    Аккаунт должен видеть источник и уметь постить в цель."""
    client = _make_client(session_string, _acc)
    forwarded = 0
    since = int(since_msg_id or 0)
    last_id = since
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        source = await _resolve_channel_peer(client, source_channel_id, 0)
        target = await _resolve_channel_peer(client, target_channel_id, 0)
        # Новая связка (курсор=0): не сваливаем старый бэклог в цель — просто
        # ставим курсор на текущий последний пост и стартуем отслеживание с «сейчас».
        if since <= 0:
            latest = await client.get_messages(source, limit=1)
            seed = int(getattr(latest[0], "id", 0)) if latest else 0
            return {"forwarded": 0, "last_msg_id": seed, "seeded": True}
        # min_id=since → только новее курсора; reverse=True → хронологически.
        msgs = []
        async for m in client.iter_messages(source, min_id=since,
                                             limit=limit, reverse=True):
            if getattr(m, "service", False):
                continue
            msgs.append(m)
        for m in msgs:
            try:
                await client.forward_messages(target, m)
                forwarded += 1
                last_id = max(last_id, int(getattr(m, "id", 0) or 0))
                await asyncio.sleep(random.uniform(1.5, 4.0))
            except Exception as e:
                log.warning("forward_new_posts fwd src=%s dst=%s: %s",
                            source_channel_id, target_channel_id, e)
        return {"forwarded": forwarded, "last_msg_id": last_id}
    except Exception as e:
        log.warning("forward_new_posts error src=%s dst=%s: %s",
                    source_channel_id, target_channel_id, e)
        return {"forwarded": forwarded, "last_msg_id": last_id, "error": str(e)[:160]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в forward_new_posts")


# ══════════════════════════════════════════════════════════════════════════════
# CONTENT OPERATIONS
# ══════════════════════════════════════════════════════════════════════════════


async def pin_last_channel_post(
    session_string: str,
    channel_id: int | str,
    access_hash: int = 0,
    username: str = "",
    _acc: dict | None = None,
    silent: bool = True,
) -> dict:
    """Закрепить последний пост в канале от имени аккаунта.

    Возвращает {"pinned_msg_id": int} при успехе либо {"error": str, ...}.
    Требует, чтобы аккаунт был админом канала с правом pin_messages.
    """
    if not session_string:
        return {"error": "session_str отсутствует — сессия недоступна"}
    from telethon.tl.types import InputPeerChannel
    from telethon.tl.functions.messages import UpdatePinnedMessageRequest
    from telethon.errors import (
        FloodWaitError,
        ChatAdminRequiredError,
        MessageIdInvalidError,
        AuthKeyUnregisteredError,
    )

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        cid = abs(int(channel_id)) if isinstance(channel_id, (int, str)) else 0
        if access_hash and cid > 0:
            peer = InputPeerChannel(channel_id=cid, access_hash=access_hash)
        elif isinstance(channel_id, str) and not channel_id.lstrip("-").isdigit():
            peer = channel_id
        elif username:
            peer = await asyncio.wait_for(
                client.get_entity(f"@{username.lstrip('@')}"), timeout=10.0)
        else:
            peer = await asyncio.wait_for(
                client.get_entity(_normalize_channel_id(channel_id)), timeout=10.0)

        msgs = await asyncio.wait_for(
            client.get_messages(peer, limit=1), timeout=_OP_TIMEOUT)
        if not msgs:
            return {"error": "В канале нет сообщений для закрепления"}
        last_id = msgs[0].id
        await asyncio.wait_for(
            client(UpdatePinnedMessageRequest(peer=peer, id=last_id, silent=silent)),
            timeout=_OP_TIMEOUT)
        return {"pinned_msg_id": last_id}
    except FloodWaitError as e:
        return {"error": f"Флуд-лимит: подождите {e.seconds}с", "flood_wait": e.seconds}
    except ChatAdminRequiredError:
        return {"error": "Требуются права администратора канала (pin_messages)", "banned": True}
    except MessageIdInvalidError:
        return {"error": "Сообщение недоступно для закрепления"}
    except AuthKeyUnregisteredError as e:
        return {"error": f"AUTH_KEY: сессия недействительна: {e}"}
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "pin")
        return {"error": "Timeout при подключении — прокси недоступен", "proxy_error": True}
    except Exception as e:
        log.warning("pin_last_channel_post error: %s", e)
        return {"error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в pin_last_channel_post")


async def post_to_channel(
    session_string: str,
    channel_id: int | str,
    text: str,
    access_hash: int = 0,
    username: str = "",
    _acc: dict | None = None,
    media_file_id: str | None = None,
    media_type: str | None = None,
    media_bytes: bytes | None = None,
    media_filename: str = "media",
) -> dict:
    """Post a text (or media+caption) message to a channel/group.

    Peer resolution order (fastest to slowest):
    1. access_hash → InputPeerChannel directly (zero API calls)
    2. username → client.get_entity("@username") (one ResolveUsername call)
    3. channel_id → client.get_entity(id) (one GetChannels call, works for public)
    4. iter_dialogs(500) fallback (slow, last resort)
    media_bytes: raw file bytes to send as media (preferred over file_id for Telethon).

    Returns {"msg_id": int} on success or {"error": str, "flood_wait"?: int} on failure.
    """
    if not session_string:
        return {"error": "session_str отсутствует — сессия недоступна"}
    from telethon.tl.types import InputPeerChannel
    from telethon.errors import (
        FloodWaitError,
        ChatWriteForbiddenError,
        UserNotParticipantError,
        UserBannedInChannelError,
        AuthKeyUnregisteredError,
        ChatAdminRequiredError,
    )

    client = _make_client(session_string, _acc)
    try:
        await _connect_and_track(client, _acc, "post")

        # Strategy 1: access_hash → direct InputPeerChannel (no API call)
        cid = abs(int(channel_id)) if isinstance(channel_id, (int, str)) else 0
        if access_hash and cid > 0:
            peer = InputPeerChannel(channel_id=cid, access_hash=access_hash)
        elif isinstance(channel_id, str) and not channel_id.lstrip("-").isdigit():
            # Strategy 2a: caller passed @username string directly
            peer = channel_id
        elif username:
            # Strategy 2b: @username from DB — single ResolveUsername API call
            uname = username.lstrip("@")
            peer = await asyncio.wait_for(client.get_entity(f"@{uname}"), timeout=10.0)
        else:
            # Strategy 3: try get_entity(numeric_id) — works for public channels
            # and channels already in Telethon's entity cache without full dialog scan
            peer = None
            try:
                peer = await asyncio.wait_for(
                    client.get_entity(_normalize_channel_id(channel_id)), timeout=10.0
                )
            except Exception as e:
                log.debug("post_to_channel: get_entity fallback failed: %s", e)

            if peer is None:
                # Strategy 4: full dialog scan (last resort, slow)
                from telethon.errors import ChannelPrivateError, ChatAdminRequiredError
                _iter = client.iter_dialogs(limit=500)
                while True:
                    try:
                        _d = await _iter.__anext__()
                    except StopAsyncIteration:
                        break
                    except (ChannelPrivateError, ChatAdminRequiredError):
                        continue
                    except Exception as e:
                        log.debug("post_to_channel: iter skip: %s", e)
                        continue
                    try:
                        eid = getattr(_d.entity, "id", None)
                    except Exception as e:
                        log.debug("post_to_channel: entity.id error: %s", e)
                        continue
                    if eid == cid:
                        peer = InputPeerChannel(
                            channel_id=cid,
                            access_hash=getattr(_d.entity, "access_hash", 0),
                        )
                        break

            if peer is None:
                return {"error": "Канал не найден в диалогах аккаунта"}

        if media_bytes and media_type:
            # Send media via raw bytes (Bot API file_ids don't work in MTProto/Telethon).
            import io as _io
            file_obj = _io.BytesIO(media_bytes)
            file_obj.name = media_filename
            msg = await asyncio.wait_for(
                client.send_file(peer, file=file_obj, caption=text, parse_mode="html"),
                timeout=_OP_TIMEOUT,
            )
        else:
            msg = await asyncio.wait_for(
                client.send_message(peer, text, parse_mode="html"),
                timeout=_OP_TIMEOUT,
            )
        # Return resolved access_hash so caller can persist it to DB (avoids repeated dialog scans)
        resolved_hash = getattr(peer, "access_hash", 0) if hasattr(peer, "access_hash") else 0
        return {"msg_id": msg.id, "resolved_access_hash": resolved_hash or 0}
    except FloodWaitError as e:
        return {"error": f"Флуд-лимит: подождите {e.seconds}с", "flood_wait": e.seconds}
    except UserBannedInChannelError as e:
        return {"error": f"Аккаунт забанен в канале: {e}", "banned": True}
    except ChatWriteForbiddenError:
        return {"error": "Нет прав для публикации (аккаунт не является админом канала)", "banned": True}
    except ChatAdminRequiredError:
        return {"error": "Требуются права администратора канала", "banned": True}
    except UserNotParticipantError:
        return {"error": "Аккаунт не является участником канала"}
    except AuthKeyUnregisteredError as e:
        return {"error": f"AUTH_KEY: сессия недействительна (другой DC или отозвана): {e}"}
    except asyncio.TimeoutError:
        _record_proxy_fail(_acc, "post")
        return {
            "error": "Timeout при подключении — прокси недоступен",
            "proxy_error": True,
        }
    except (OSError, ConnectionError) as e:
        _record_proxy_fail(_acc, "post")
        return {"error": f"Ошибка сети (прокси?): {e}", "proxy_error": True}
    except Exception as e:
        log.warning("post_to_channel error: %s", e)
        return {"error": str(e)[:150]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в post_to_channel")


async def send_reaction(
    session_string: str,
    channel_id: int | str,
    msg_id: int,
    emoji: str,
    _acc: dict | None = None,
) -> bool:
    """Send a reaction emoji to a specific message."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(channel_id)
        await client(
            SendReactionRequest(
                peer=entity,
                msg_id=msg_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
            )
        )
        return True
    except Exception as e:
        log.exception("send_reaction error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в send_reaction")


async def report_peer(
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
    _acc: dict | None = None,
) -> bool:
    """Report a channel/user to Telegram moderators.

    reason: 'spam' | 'violence' | 'pornography' | 'childabuse' | 'copyright' | 'other'
    """
    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.types import (
        InputReportReasonSpam,
        InputReportReasonViolence,
        InputReportReasonPornography,
        InputReportReasonChildAbuse,
        InputReportReasonCopyright,
        InputReportReasonOther,
    )

    reason_map = {
        "spam": InputReportReasonSpam(),
        "violence": InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse": InputReportReasonChildAbuse(),
        "copyright": InputReportReasonCopyright(),
        "other": InputReportReasonOther(),
    }
    tg_reason = reason_map.get(reason, InputReportReasonSpam())
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        await client(ReportPeerRequest(peer=entity, reason=tg_reason, message=message))
        return True
    except Exception as e:
        log.exception("report_peer error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer")


async def report_peer_deep(
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
    msg_messages: list[str] | None = None,
    max_msg_reports: int = 50,
    block_after: bool = True,
    multi_reason: bool = True,
    join_first: bool = True,
    negative_react: bool = True,
    report_admins: bool = True,
    report_linked_bots: bool = True,
    forward_to_bot: bool = True,
    report_photo: bool = True,
    report_pinned: bool = True,
    report_linked_group: bool = True,
    _acc: dict | None = None,
) -> dict:
    """12-векторная атака на нелегальный ресурс за одно подключение.

    1.  ReportPeer — все доступные причины по кругу (primary + все вторичные)
    2.  ReportProfilePhoto — жалоба на фото профиля канала
    3.  JoinChannel — войти для отчётов изнутри (весят больше)
    4.  Pinned Messages — ReportRequest (закреплённые = приоритет для модераторов)
    5.  Regular Messages — ReportRequest на 50 последних (чанки по 5, все причины)
    6.  channels.ReportSpam — дополнительный спам-сигнал
    7.  Negative Reactions 👎💩 на все доступные посты (до 20)
    8.  Admins — ReportPeer на ВСЕХ администраторов
    9.  Linked Group — ReportPeer на связанную группу обсуждений
    10. Linked Bots — ReportPeer на боты из описания/постов
    11. Forward Evidence → @stopCA / @notoscam
    12. Block + Mute + Leave
    """
    import re as _re

    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.functions.messages import ReportRequest as MsgReportRequest
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import (
        JoinChannelRequest,
        LeaveChannelRequest,
        GetParticipantsRequest,
        GetFullChannelRequest,
    )
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        InputReportReasonSpam,
        InputReportReasonViolence,
        InputReportReasonPornography,
        InputReportReasonChildAbuse,
        InputReportReasonCopyright,
        InputReportReasonOther,
        Channel,
        ChannelParticipantsAdmins,
        ReactionEmoji,
        InputMessagesFilterPinned,
    )

    # Optional imports — newer TL layers / Telethon versions
    try:
        import importlib.util

        _has_photo_report = (
            importlib.util.find_spec("telethon.tl.functions.account") is not None
        )
    except (ImportError, ValueError):
        _has_photo_report = False

    _has_chan_spam = False
    ChanSpamRequest = None
    try:
        from telethon.tl.functions.channels import ReportSpamRequest as _CSR

        ChanSpamRequest = _CSR
        _has_chan_spam = True
    except ImportError:
        pass

    # Build reason map — try to include newer TL types
    reason_map: dict = {
        "spam": InputReportReasonSpam(),
        "violence": InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse": InputReportReasonChildAbuse(),
        "copyright": InputReportReasonCopyright(),
        "other": InputReportReasonOther(),
    }
    for _type_name, _key in [
        ("InputReportReasonIllegalDrugs", "drugs"),
        ("InputReportReasonPersonalDetails", "personal"),
        ("InputReportReasonFake", "fake"),
        ("InputReportReasonGeoIrrelevant", "geo"),
    ]:
        try:
            import telethon.tl.types as _tlt

            reason_map[_key] = getattr(_tlt, _type_name)()
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer_deep")
    # Escalation: primary → all applicable secondary reasons
    _escalation: dict[str, list[str]] = {
        "childabuse": ["pornography", "violence", "drugs", "spam", "other"],
        "drugs": ["childabuse", "violence", "spam", "other"],
        "violence": ["childabuse", "spam", "drugs", "fake", "other"],
        "pornography": ["childabuse", "spam", "other", "violence"],
        "spam": ["other", "violence", "personal", "fake"],
        "other": ["spam", "violence", "pornography", "drugs"],
        "copyright": ["spam", "other"],
    }

    _report_bots: dict[str, str] = {
        "childabuse": "stopCA",
        "drugs": "stopCA",
        "violence": "notoscam",
        "other": "notoscam",
        "spam": "notoscam",
        "pornography": "notoscam",
    }

    tg_reason = reason_map.get(reason, InputReportReasonOther())
    # Build ordered reason cycle: primary first, then all secondary
    all_reasons_cycle = [tg_reason]
    for sec_key in _escalation.get(reason, []):
        if sec_key in reason_map:
            all_reasons_cycle.append(reason_map[sec_key])

    result = {
        "peer_reported": False,
        "multi_reason_sent": 0,
        "photo_reported": False,
        "pinned_reported": 0,
        "msg_reported": 0,
        "spam_signaled": 0,
        "reactions_sent": 0,
        "admins_reported": 0,
        "linked_group_reported": False,
        "bots_reported": 0,
        "forwarded": 0,
        "blocked": False,
        "joined": False,
    }
    msg_pool = msg_messages or [message] or [""]

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        is_channel = isinstance(entity, Channel)

        # ── 1. ReportPeer — все причины по кругу ──────────────────────────
        for idx, r_obj in enumerate(all_reasons_cycle if multi_reason else [tg_reason]):
            try:
                if idx > 0:
                    await asyncio.sleep(0.35)
                await client(
                    ReportPeerRequest(
                        peer=entity,
                        reason=r_obj,
                        message=msg_pool[idx % len(msg_pool)],
                    )
                )
                if idx == 0:
                    result["peer_reported"] = True
                else:
                    result["multi_reason_sent"] += 1
            except Exception as e:
                log.warning("report_peer_deep[1/peer idx=%d]: %s", idx, e)

        # ── 2. Report Profile Photo ────────────────────────────────────────
        if report_photo and _has_photo_report:
            try:
                from telethon.tl.functions.account import (
                    ReportProfilePhotoRequest as _RPP,
                )

                photos = await client.get_profile_photos(entity, limit=1)
                if photos:
                    await client(
                        _RPP(
                            peer=entity,
                            photo_id=client._get_input_photo(photos[0]),
                            reason=tg_reason,
                            message=msg_pool[0],
                        )
                    )
                    result["photo_reported"] = True
            except Exception as e:
                log.warning("report_peer_deep[2/photo]: %s", e)

        # ── 3. Вступить в канал для утяжелённых отчётов ───────────────────
        if join_first and is_channel:
            try:
                await client(JoinChannelRequest(entity))
                result["joined"] = True
                # Обновляем entity после join — access hash обновляется в сессии
                entity = await client.get_entity(peer_username.lstrip("@"))
                await asyncio.sleep(random.uniform(2.0, 4.5))
            except Exception as e:
                log.warning("report_peer_deep[3/join]: %s", e)

        # Get full channel info (linked group, about text)
        full_chat = None
        try:
            full_result = await client(GetFullChannelRequest(entity))
            full_chat = full_result.full_chat
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer_deep")
        # ── 4. Pinned messages — высший приоритет для модераторов ─────────
        pinned_msgs = []
        if report_pinned and is_channel:
            try:
                pinned_msgs = await client.get_messages(
                    entity, filter=InputMessagesFilterPinned(), limit=20
                )
                pinned_ids = [m.id for m in pinned_msgs if m and m.id]
                for idx_p, pid in enumerate(pinned_ids):
                    try:
                        ok = await _submit_message_report(
                            client,
                            MsgReportRequest,
                            entity,
                            [pid],
                            msg_pool[idx_p % len(msg_pool)],
                            reason,
                            f"report_peer_deep[4/pinned {pid}]",
                        )
                        if ok:
                            result["pinned_reported"] += 1
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        log.warning("report_peer_deep[4/pinned %d]: %s", pid, e)
            except Exception as e:
                log.warning("report_peer_deep[4/get_pinned]: %s", e)

        # ── 5. Жалобы на последние 50 сообщений (все причины по кругу) ────
        msgs: list = []
        if is_channel:
            try:
                msgs = await client.get_messages(entity, limit=max_msg_reports)
                msg_ids = [m.id for m in msgs if m and m.id]
                chunks = [msg_ids[i : i + 5] for i in range(0, len(msg_ids), 5)]
                for chunk_idx, chunk in enumerate(chunks):
                    chunk_msg = msg_pool[chunk_idx % len(msg_pool)]
                    try:
                        ok = await _submit_message_report(
                            client,
                            MsgReportRequest,
                            entity,
                            chunk,
                            chunk_msg,
                            reason,
                            f"report_peer_deep[5/msg_chunk {chunk_idx}]",
                        )
                        if ok:
                            result["msg_reported"] += len(chunk)
                    except Exception as e:
                        log.warning(
                            "report_peer_deep[5/msg_chunk %d]: %s", chunk_idx, e
                        )
                    await asyncio.sleep(0.55)
            except Exception as e:
                log.warning("report_peer_deep[5/get_msgs]: %s", e)

        # ── 6. channels.ReportSpam (отдельный спам-сигнал) ────────────────
        if _has_chan_spam and ChanSpamRequest and msgs and is_channel:
            spam_ids = [m.id for m in msgs[:10] if m and m.id]
            if spam_ids:
                try:
                    await client(
                        ChanSpamRequest(
                            channel=entity,
                            participant=entity,
                            id=spam_ids,
                        )
                    )
                    result["spam_signaled"] += len(spam_ids)
                except Exception as e:
                    log.warning("report_peer_deep[6/chan_spam]: %s", e)

        # ── 7. Негативные реакции на все доступные посты ──────────────────
        if negative_react and msgs:
            reaction_emojis = ["👎", "💩", "🤮"]
            for r_idx, m in enumerate(msgs[:20]):
                if not (m and m.id):
                    continue
                emoji = reaction_emojis[r_idx % len(reaction_emojis)]
                try:
                    await client(
                        SendReactionRequest(
                            peer=entity,
                            msg_id=m.id,
                            reaction=[ReactionEmoji(emoticon=emoji)],
                        )
                    )
                    result["reactions_sent"] += 1
                    await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning("report_peer_deep[7/react]: %s", e)

        # ── 8. Жалобы на ВСЕХ администраторов ────────────────────────────
        if report_admins and is_channel:
            try:
                admins_result = await client(
                    GetParticipantsRequest(
                        channel=entity,
                        filter=ChannelParticipantsAdmins(),
                        offset=0,
                        limit=50,
                        hash=0,
                    )
                )
                admin_users = getattr(admins_result, "users", [])
                for a_idx, usr in enumerate(admin_users):
                    try:
                        await asyncio.sleep(0.4)
                        r_obj = all_reasons_cycle[a_idx % len(all_reasons_cycle)]
                        await client(
                            ReportPeerRequest(
                                peer=usr,
                                reason=r_obj,
                                message=msg_pool[a_idx % len(msg_pool)],
                            )
                        )
                        result["admins_reported"] += 1
                    except Exception as e:
                        log.warning("report_peer_deep[8/admin]: %s", e)
            except Exception as e:
                log.warning("report_peer_deep[8/get_admins]: %s", e)

        # ── 9. Linked discussion group ────────────────────────────────────
        if report_linked_group and full_chat:
            linked_id = getattr(full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    linked_entity = await client.get_entity(int(linked_id))
                    for idx_lg, r_obj in enumerate(all_reasons_cycle[:3]):
                        try:
                            await asyncio.sleep(0.5)
                            await client(
                                ReportPeerRequest(
                                    peer=linked_entity,
                                    reason=r_obj,
                                    message=msg_pool[idx_lg % len(msg_pool)],
                                )
                            )
                            result["linked_group_reported"] = True
                        except Exception as e:
                            log.warning(
                                "report_peer_deep[9/linked reason %d]: %s", idx_lg, e
                            )
                except Exception as e:
                    log.warning("report_peer_deep[9/get_linked]: %s", e)

        # ── 10. Linked bots → ReportPeer ──────────────────────────────────
        if report_linked_bots and is_channel:
            bot_re = _re.compile(r"@([A-Za-z]\w{4,31}[Bb]ot)\b")
            scan_text = ""
            if full_chat:
                scan_text += (getattr(full_chat, "about", "") or "") + " "
            for m in msgs[:5]:
                if m and m.text:
                    scan_text += m.text + " "
            found_bots = list(set(bot_re.findall(scan_text)))[:5]
            for b_idx, bot_uname in enumerate(found_bots):
                try:
                    bot_entity = await client.get_entity(bot_uname)
                    r_obj = all_reasons_cycle[b_idx % len(all_reasons_cycle)]
                    await client(
                        ReportPeerRequest(
                            peer=bot_entity,
                            reason=r_obj,
                            message=msg_pool[b_idx % len(msg_pool)],
                        )
                    )
                    result["bots_reported"] += 1
                    await asyncio.sleep(0.5)
                except Exception as e:
                    log.warning("report_peer_deep[10/bot %s]: %s", bot_uname, e)

        # ── 11. Forward evidence → @stopCA / @notoscam ─────────────────────
        if forward_to_bot and msgs:
            bot_username = _report_bots.get(reason, "notoscam")
            try:
                bot_ent = await client.get_entity(bot_username)
                evidence_msgs = [m for m in msgs[:5] if m and not m.service]
                for em in evidence_msgs:
                    try:
                        await client.forward_messages(bot_ent, em)
                        result["forwarded"] += 1
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        log.warning("report_peer_deep[11/fwd]: %s", e)
            except Exception as e:
                log.warning("report_peer_deep[11/get_bot]: %s", e)

        # ── 12. Mute + Block + Leave ───────────────────────────────────────
        try:
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

            await client(
                UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer=entity),
                    settings=InputPeerNotifySettings(
                        mute_until=_TELEGRAM_MAX_MUTE_UNTIL
                    ),
                )
            )
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer_deep")
        if result["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception:
                log_exc_swallow(log, "Сбой в report_peer_deep")
        if block_after:
            try:
                await client(BlockRequest(id=entity))
                result["blocked"] = True
            except Exception as e:
                log.warning("report_peer_deep[12/block]: %s", e)

    except Exception as e:
        log.exception("report_peer_deep error: %s", e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в report_peer_deep")
    return result


async def report_peer_deep_v2(  # noqa: C901
    session_string: str,
    peer_username: str,
    reason: str,
    message: str = "",
    msg_messages: list[str] | None = None,
    max_msg_reports: int = 60,
    block_after: bool = True,
    multi_reason: bool = True,
    join_first: bool = True,
    negative_react: bool = True,
    report_admins: bool = True,
    report_linked_bots: bool = True,
    forward_to_bot: bool = True,
    report_photo: bool = True,
    report_pinned: bool = True,
    report_linked_group: bool = True,
    wave_num: int = 0,
    _acc: dict | None = None,
) -> dict:
    """Bulletproof 12-vector deep strike. Every vector isolated, entity refreshed after join."""
    import re as _re

    from telethon.tl.functions.account import ReportPeerRequest
    from telethon.tl.functions.messages import ReportRequest as MsgReportRequest
    from telethon.tl.functions.contacts import BlockRequest
    from telethon.tl.functions.channels import (
        JoinChannelRequest,
        LeaveChannelRequest,
        GetParticipantsRequest,
        GetFullChannelRequest,
    )
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import (
        InputReportReasonSpam,
        InputReportReasonViolence,
        InputReportReasonPornography,
        InputReportReasonChildAbuse,
        InputReportReasonCopyright,
        InputReportReasonOther,
        Channel,
        ChannelParticipantsAdmins,
        ReactionEmoji,
        InputMessagesFilterPinned,
        ReportResultAddComment,
        ReportResultChooseOption,
        ReportResultReported,
    )

    _RPP = None
    try:
        from telethon.tl.functions.account import ReportProfilePhotoRequest as _RPP
    except ImportError:
        pass

    _CSR = None
    try:
        from telethon.tl.functions.channels import ReportSpamRequest as _CSR
    except ImportError:
        pass

    # ── Result ────────────────────────────────────────────────────────────
    R: dict = {
        "peer_reported": False,
        "multi_reason_sent": 0,
        "photo_reported": False,
        "pinned_reported": 0,
        "msg_reported": 0,
        "msgs_fetched": 0,
        "spam_signaled": 0,
        "reactions_sent": 0,
        "admins_reported": 0,
        "linked_group_reported": False,
        "bots_reported": 0,
        "forwarded": 0,
        "blocked": False,
        "_entity_error": False,  # entity could not be resolved — no actions taken
        "joined": False,
        "rate_limited": False,
        "_peer_flood": False,
        "_fatal": False,
        "_long_flood": False,
        "errors": [],
    }

    # ── Reason map ────────────────────────────────────────────────────────
    _rm: dict = {
        "spam": InputReportReasonSpam(),
        "violence": InputReportReasonViolence(),
        "pornography": InputReportReasonPornography(),
        "childabuse": InputReportReasonChildAbuse(),
        "copyright": InputReportReasonCopyright(),
        "other": InputReportReasonOther(),
    }
    for _tn, _rk in [
        ("InputReportReasonIllegalDrugs", "drugs"),
        ("InputReportReasonPersonalDetails", "personal"),
        ("InputReportReasonFake", "fake"),
        ("InputReportReasonGeoIrrelevant", "geo"),
    ]:
        try:
            import telethon.tl.types as _tlt

            _rm[_rk] = getattr(_tlt, _tn)()
        except Exception as e:
            log.debug("rpv2: reason_map import %s: %s", _tn, e)

    _escalation: dict[str, list[str]] = {
        "childabuse": ["pornography", "violence", "drugs", "spam", "other"],
        "csam": ["pornography", "violence", "drugs", "spam", "other"],
        "drugs": ["childabuse", "violence", "spam", "other"],
        "violence": ["childabuse", "spam", "drugs", "fake", "other"],
        "terrorism": ["childabuse", "violence", "spam", "drugs", "other"],
        "pornography": ["childabuse", "spam", "other", "violence"],
        "escort": ["pornography", "childabuse", "spam", "other"],
        "spam": ["other", "violence", "personal", "fake"],
        "other": ["spam", "violence", "pornography", "drugs"],
        "copyright": ["spam", "other"],
        "fraud": ["spam", "other", "fake", "violence"],
        "weapons": ["violence", "spam", "other"],
        "darknet": ["spam", "other", "drugs"],
    }
    # Несколько ботов-получателей доказательств — по типу нарушения.
    # Пересылка в несколько независимых инстанций усиливает сигнал.
    _fwd_bots_multi: dict[str, list[str]] = {
        "childabuse": ["stopCA", "notoscam"],
        "csam": ["stopCA", "notoscam"],
        "drugs": ["stopCA", "notoscam"],
        "violence": ["notoscam", "stopCA"],
        "fraud": ["notoscam"],
        "escort": ["stopCA", "notoscam"],
        "spam": ["notoscam"],
        "pornography": ["notoscam", "stopCA"],
        "other": ["notoscam"],
    }

    tg_reason = _rm.get(reason, InputReportReasonOther())
    all_reasons: list = [tg_reason] + [
        _rm[k] for k in _escalation.get(reason, []) if k in _rm
    ]
    _raw_pool: list[str] = msg_messages or ([message] if message else [])
    msg_pool: list[str] = [t for t in _raw_pool if t.strip()] or [
        "Report: inappropriate content"
    ]
    _ref_kind, _ref_val = normalize_telegram_join_ref(peer_username)
    peer = f"+{_ref_val}" if _ref_kind == "invite" else _ref_val.lstrip("@")
    acc_id = (_acc or {}).get("id", "?")

    def _record_error(stage: str, err: object) -> None:
        text = str(err)[:120]
        R["errors"].append(f"{stage}: {text}")
        up = text.upper()
        if "FLOOD" in up or "TOO_MUCH" in up:
            R["rate_limited"] = True
        # PEER_FLOOD — account-level spam signal: must STOP, not keep reporting.
        # Continuing write actions after PEER_FLOOD escalates to a hard ban.
        if "PEER_FLOOD" in up:
            R["_peer_flood"] = True
        # Fatal: session revoked / account banned → stop immediately.
        if any(
            m in up
            for m in (
                "AUTH_KEY_UNREGISTERED",
                "SESSION_REVOKED",
                "USER_DEACTIVATED",
                "PHONE_NUMBER_BANNED",
            )
        ):
            R["_fatal"] = True

    def _abort_signal() -> bool:
        """True if account hit PEER_FLOOD/ban/long-flood — skip remaining vectors."""
        return bool(R["_peer_flood"] or R["_fatal"] or R["_long_flood"])

    # Floods longer than this within a single strike are not worth blocking on —
    # we cool the account (via flood_engine at the caller) and abort the rest.
    _MAX_INLINE_FLOOD = 180.0

    def _flood(err: str, default: float = 30.0) -> float:
        # Return the real wait the loop should sleep. For waits the strike can
        # absorb (≤ _MAX_INLINE_FLOOD) sleep exactly; for longer ones, flag
        # rate-limited + long-flood so vectors abort instead of blocking for hours.
        m = _re.search(r"(\d+)", err)
        real = float(m.group(1)) if m else default
        if real > _MAX_INLINE_FLOOD:
            R["rate_limited"] = True
            R["_long_flood"] = True
            return _MAX_INLINE_FLOOD
        return real

    async def _timed(coro, timeout: float = 15.0):
        return await asyncio.wait_for(coro, timeout=timeout)

    def _select_report_option(options: list) -> bytes | None:
        """Pick best-matching option by reason. Returns bytes option value or None."""
        hints = {
            "spam": (
                "spam",
                "спам",
                "unwanted",
                "advertising",
                "реклам",
                "unsolicited",
            ),
            "violence": (
                "violence",
                "violent",
                "насил",
                "жест",
                "жестокост",
                "harm",
                "abuse",
                "hurt",
            ),
            "pornography": (
                "porn",
                "sexual",
                "adult",
                "порно",
                "сексу",
                "18+",
                "explicit",
                "nudity",
            ),
            "childabuse": (
                "child",
                "minor",
                "children",
                "дет",
                "несовершен",
                "csam",
                "underage",
                "abuse",
            ),
            "copyright": ("copyright", "автор", "dmca", "intellectual", "авторск"),
            "drugs": (
                "drug",
                "нарко",
                "substance",
                "наркотик",
                "нарк",
                "illegal substance",
                "narcotic",
            ),
            "personal": ("personal", "private", "личн", "privacy", "данн"),
            "fake": (
                "fake",
                "scam",
                "fraud",
                "фейк",
                "мошен",
                "impersonat",
                "phishing",
            ),
            "other": ("other", "другое", "else", "иное", "прочее"),
            "weapons": ("weapon", "оружи", "arms", "firearm", "explosive"),
            "terrorism": ("terror", "extremi", "террор", "экстрем", "incit"),
            "fraud": ("fraud", "scam", "мошен", "financial", "финанс"),
            "escort": ("escort", "prostit", "услуг", "сексуальн"),
            "geo": ("geo", "irrelevant", "geography", "геогр", "not relevant"),
        }.get(reason, ())
        opt_texts = [f"'{(getattr(o, 'text', '') or '')}'" for o in options]
        log.debug("rpv2 option_select reason=%s available=%s", reason, opt_texts)
        for opt in options:
            text = (getattr(opt, "text", "") or "").lower()
            if any(hint in text for hint in hints):
                val = getattr(opt, "option", None)
                if val is not None:
                    return val
        # Fallback: первый доступный вариант
        if options:
            val = getattr(options[0], "option", None)
            if val is not None:
                return val
        log.warning("rpv2 option_select: no option bytes in %s", opt_texts)
        return None

    async def _report_message_ids(
        peer_obj, msg_ids: list[int], comment: str, stage: str
    ) -> bool:
        """BFS через все доступные опции жалобы. Пробует ВСЕ пути до ReportResultReported.
        В отличие от линейного перебора — не сдаётся при первом несовпадении.
        """

        async def _traverse(opt_bytes: bytes, depth: int) -> bool:
            if depth > 5:
                return False
            try:
                result = await _timed(
                    client(
                        MsgReportRequest(
                            peer=peer_obj,
                            id=msg_ids,
                            option=opt_bytes,
                            message=comment if depth > 0 else "",
                        )
                    ),
                    20.0,
                )
            except Exception as e:
                err = str(e)
                _record_error(stage, e)
                if "FLOOD_WAIT" in err.upper():
                    await asyncio.sleep(_flood(err))
                return False
            if isinstance(result, ReportResultReported):
                return True
            if isinstance(result, ReportResultAddComment):
                try:
                    final = await _timed(
                        client(
                            MsgReportRequest(
                                peer=peer_obj,
                                id=msg_ids,
                                option=result.option,
                                message=comment,
                            )
                        ),
                        15.0,
                    )
                    return isinstance(final, ReportResultReported)
                except Exception as e:
                    _record_error(stage, e)
                    return False
            if isinstance(result, ReportResultChooseOption):
                opts = result.options or []
                # Сначала пробуем лучшее совпадение, затем остальные по порядку
                best_bytes = _select_report_option(opts)
                ordered: list[bytes] = []
                if best_bytes is not None:
                    ordered.append(best_bytes)
                for o in opts:
                    v = getattr(o, "option", None)
                    if v is not None and v != best_bytes:
                        ordered.append(v)
                for opt_val in ordered:
                    await asyncio.sleep(random.uniform(0.4, 1.0))
                    if await _traverse(opt_val, depth + 1):
                        return True
            return False

        return await _traverse(b"", 0)

    client = _make_client(session_string, _acc)
    try:
        await _timed(client.connect(), _CONNECT_TIMEOUT)

        # Resolve entity — без этого вся атака невозможна
        # Для приватных invite-ссылок сразу вступаем через ImportChatInviteRequest.
        # Поддерживаемые форматы: +HASH, t.me/+HASH, https://t.me/+HASH,
        #   t.me/joinchat/HASH, https://telegram.me/joinchat/HASH
        import re as _re_inv
        _invite_hash: str | None = None
        _peer_s = peer.strip()
        if _peer_s.startswith("+") and not _peer_s.lstrip("+").isdigit():
            _invite_hash = _peer_s.lstrip("+")
        else:
            _m_inv = _re_inv.match(
                r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)",
                _peer_s,
            )
            if _m_inv:
                _invite_hash = _m_inv.group(1)
        try:
            if _invite_hash:
                from telethon.tl.functions.messages import (
                    ImportChatInviteRequest as _ICIR,
                )

                try:
                    _inv_result = await _timed(client(_ICIR(hash=_invite_hash)), 20.0)
                    entity = _inv_result.chats[0]
                    R["joined"] = True
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    # Refresh via InputChannel (id+access_hash) — bare integer ID fails
                    # for supergroups/channels without access_hash in the Telethon lookup.
                    try:
                        from telethon.tl.types import InputChannel as _IC_inv
                        _ie_inv = _IC_inv(entity.id, getattr(entity, "access_hash", 0))
                        entity = await _timed(client.get_entity(_ie_inv), 15.0)
                    except Exception as e:
                        log.debug("rpv2: entity refresh from join failed: %s", e)
                except Exception as e:
                    log.warning("rpv2: join invite failed (falling back to CheckChatInvite): %s", e)
                    # Already a member (or other join error) — use CheckChatInviteRequest
                    # to get entity. ImportChatInviteRequest fails for existing members,
                    # but get_entity("+hash") mis-parses the hash as a phone number.
                    try:
                        from telethon.tl.functions.messages import (
                            CheckChatInviteRequest as _CCIR,
                        )
                        from telethon.tl.types import ChatInviteAlready as _CIA

                        _check = await _timed(client(_CCIR(hash=_invite_hash)), 10.0)
                        if isinstance(_check, _CIA):
                            entity = _check.chat
                            R["joined"] = True
                        elif hasattr(_check, "chat") and _check.chat:
                            entity = _check.chat
                        else:
                            raise ValueError("CheckChatInvite returned no chat")
                    except Exception as e:
                        log.warning("rpv2: CheckChatInvite fallback failed: %s", e)
                        if peer.startswith("+"):
                            # peer is "+HASH" — get_entity mis-parses "+" as phone prefix
                            raise
                        entity = await _timed(client.get_entity(peer), 15.0)
            else:
                entity = await _timed(client.get_entity(peer))
        except Exception as e:
            log.warning("rpv2[0/entity] acc=%s target=%s: %s", acc_id, peer, e)
            R["_entity_error"] = True
            return R

        is_channel = isinstance(entity, Channel)
        log.info(
            "rpv2 start acc=%s target=%s is_channel=%s wave=%d",
            acc_id,
            peer,
            is_channel,
            wave_num,
        )

        # ── A. Pre-fetch history (АНОНИМНО, до вступления) ──────────────
        # Публичные каналы читаемы без вступления. Получаем историю ДО join-а,
        # чтобы обойти anti-bot защиту (CAS/ComBot), банящую новых участников.
        _prefetch_msgs: list = []
        if is_channel:
            try:
                from telethon.tl.functions.messages import GetHistoryRequest as _GHR_PRE

                _pre_hist = await _timed(
                    client(
                        _GHR_PRE(
                            peer=entity,
                            offset_id=0,
                            offset_date=None,
                            add_offset=0,
                            limit=max_msg_reports,
                            max_id=0,
                            min_id=0,
                            hash=0,
                        )
                    ),
                    20.0,
                )
                _prefetch_msgs = [
                    m
                    for m in getattr(_pre_hist, "messages", [])
                    if m and m.id and not getattr(m, "action", None)
                ]
                if _prefetch_msgs:
                    log.info(
                        "rpv2[2.5] anon_prefetch=%d acc=%s target=%s",
                        len(_prefetch_msgs),
                        acc_id,
                        peer,
                    )
            except Exception as _pre_e:
                log.debug("rpv2[2.5] skipped acc=%s: %s", acc_id, str(_pre_e)[:60])

        # ── 3. Join channel — ОБЯЗАТЕЛЬНО до message reporting ────────
        if join_first and is_channel and not R["joined"]:
            _need_refresh = True  # нужен ли дополнительный entity-refresh
            try:
                await asyncio.sleep(random.uniform(0.3, 0.8))
                _join_resp = await _timed(client(JoinChannelRequest(entity)))
                R["joined"] = True
                log.info("rpv2[3] joined acc=%s target=%s", acc_id, peer)
                # PRIMARY: ответ JoinChannelRequest содержит актуальный entity из сервера —
                # это надёжнее отдельного GetChannelsRequest (нет проблем с кэшем).
                _fresh = getattr(_join_resp, "chats", [])
                if _fresh:
                    # Ищем наш канал по ID — в chats[1] может быть linked group
                    _matched = next(
                        (c for c in _fresh if getattr(c, "id", None) == entity.id),
                        _fresh[0],
                    )
                    entity = _matched
                    _need_refresh = False
                    log.info(
                        "rpv2[3] entity from join_resp acc=%s ah=%s",
                        acc_id,
                        getattr(entity, "access_hash", "?"),
                    )
                await asyncio.sleep(
                    random.uniform(3.0, 7.0)
                    if wave_num == 0
                    else random.uniform(1.0, 2.5)
                )
            except Exception as e:
                err = str(e)
                if "ALREADY_PARTICIPANT" in err.upper() or "already" in err.lower():
                    R["joined"] = True
                    log.info(
                        "rpv2[3] already_participant acc=%s target=%s", acc_id, peer
                    )
                else:
                    log.warning(
                        "rpv2[3/join] acc=%s target=%s: %s", acc_id, peer, err[:100]
                    )
            # FALLBACK refresh: когда join не дал свежий entity (join_resp.chats пустой,
            # ALREADY_PARTICIPANT, или join упал).
            # Используем get_input_entity — берёт access_hash из сессии (надёжнее для членов).
            if _need_refresh:
                try:
                    from telethon.tl.functions.channels import (
                        GetChannelsRequest as _GCR,
                    )

                    # get_input_entity для канала в котором аккаунт уже состоит
                    # возвращает InputChannel из session cache с правильным access_hash
                    _ie = None
                    try:
                        _ie = await _timed(client.get_input_entity(peer), 5.0)
                    except Exception as _gie:
                        log.warning("rpv2[3/gie] acc=%s: %s", acc_id, str(_gie)[:60])
                        from telethon.tl.types import InputChannel as _IC

                        _ie = _IC(entity.id, entity.access_hash)
                    _gcr = await _timed(client(_GCR([_ie])), 10.0)
                    if _gcr and _gcr.chats:
                        entity = _gcr.chats[0]
                        log.info(
                            "rpv2[3/gcr] entity refreshed acc=%s ah=%s",
                            acc_id,
                            getattr(entity, "access_hash", "?"),
                        )
                    else:
                        raise ValueError("gcr empty")
                except Exception as _gcr_e:
                    log.warning(
                        "rpv2[3/gcr] acc=%s: %s — fallback get_entity",
                        acc_id,
                        str(_gcr_e)[:80],
                    )
                    try:
                        entity = await _timed(client.get_entity(peer), 8.0)
                        log.info(
                            "rpv2[3/get_entity] acc=%s ah=%s",
                            acc_id,
                            getattr(entity, "access_hash", "?"),
                        )
                    except Exception as e2:
                        log.warning(
                            "rpv2[3/get_entity] acc=%s: %s", acc_id, str(e2)[:80]
                        )

        # ── 4. Full channel info ───────────────────────────────────────
        full_chat = None
        if is_channel:
            try:
                fc_res = await _timed(client(GetFullChannelRequest(entity)))
                full_chat = fc_res.full_chat
                # GetFullChannelRequest тоже возвращает chats — ещё один refresh point
                _fc_chats = getattr(fc_res, "chats", [])
                if _fc_chats:
                    _fc_match = next(
                        (c for c in _fc_chats if getattr(c, "id", None) == entity.id),
                        _fc_chats[0],
                    )
                    entity = _fc_match
                    log.info(
                        "rpv2[4] entity from GetFullChannel acc=%s ah=%s",
                        acc_id,
                        getattr(entity, "access_hash", "?"),
                    )
            except Exception as e:
                log.warning("rpv2[4/full] acc=%s: %s", acc_id, str(e)[:80])

        # ── 5. GetHistory (fetch) ─────────────────────────────────────
        # Загружаем сообщения ПЕРВЫМ делом после GetFullChannel.
        # История нужна для browse-фазы (view increment), реакций и жалоб.
        # Явный InputPeerChannel обходит кэш Telethon после join.
        msgs: list = []
        _ipeer6 = None
        if is_channel:
            from telethon.tl.functions.messages import GetHistoryRequest as _GHR
            from telethon.tl.types import InputPeerChannel as _IPC6

            _ah6 = getattr(entity, "access_hash", 0)
            _ipeer6 = _IPC6(channel_id=entity.id, access_hash=_ah6)
            log.info(
                "rpv2[5/fetch] InputPeerChannel id=%s ah=%s joined=%s acc=%s",
                entity.id,
                _ah6,
                R["joined"],
                acc_id,
            )
            try:
                _hist = await _timed(
                    client(
                        _GHR(
                            peer=_ipeer6,
                            offset_id=0,
                            offset_date=None,
                            add_offset=0,
                            limit=max_msg_reports,
                            max_id=0,
                            min_id=0,
                            hash=0,
                        )
                    ),
                    25.0,
                )
                msgs = [
                    m
                    for m in getattr(_hist, "messages", [])
                    if m and m.id and not getattr(m, "action", None)
                ]
                log.info(
                    "rpv2[5/GetHistory] fetched=%d target=%s acc=%s joined=%s",
                    len(msgs),
                    peer,
                    acc_id,
                    R["joined"],
                )
            except Exception as _gh_e:
                log.warning(
                    "rpv2[5/GetHistory] acc=%s: %s — fallback", acc_id, str(_gh_e)[:80]
                )
                try:
                    raw = await _timed(
                        client.get_messages(_ipeer6, limit=max_msg_reports), 20.0
                    )
                    msgs = [
                        m
                        for m in (raw or [])
                        if m and m.id and not getattr(m, "action", None)
                    ]
                except Exception as e:
                    log.debug("rpv2[5/GetHistory] fallback failed: %s", e)
            if not msgs:
                try:
                    raw2 = await _timed(
                        client.get_messages(entity, limit=max_msg_reports), 20.0
                    )
                    msgs = [
                        m for m in (raw2 or []) if m and not getattr(m, "action", None)
                    ]
                except Exception as e:
                    log.debug("rpv2[5/GetHistory] second fallback failed: %s", e)
            if not msgs and _prefetch_msgs:
                msgs = _prefetch_msgs
                log.info(
                    "rpv2[5/pre_fallback] using anon pre-join msgs=%d acc=%s",
                    len(msgs),
                    acc_id,
                )
            R["msgs_fetched"] = len(msgs)
            if not msgs:
                log.warning(
                    "rpv2[5] 0 msgs target=%s acc=%s — channel may restrict history",
                    peer,
                    acc_id,
                )

        # ── 6. BROWSE PHASE: scroll → view → react ────────────────────
        # Правильный порядок действий реального пользователя:
        #   Открыл канал → полистал посты → прочитал → возмутился →
        #   поставил дизлайк → нажал "Пожаловаться"
        #
        # GetMessagesViewsRequest(increment=True) — PER POST, с задержкой чтения.
        # Telegram считает view-сигнал только при индивидуальных вызовах с паузами.
        # Батч из 25 постов одним запросом ≠ "пользователь прочитал 25 постов".
        if msgs and is_channel:
            _GMV = None
            try:
                from telethon.tl.functions.messages import (
                    GetMessagesViewsRequest as _GMV,
                )
            except ImportError:
                pass

            _react_pools = [
                ["👎", "💩", "🤮"],
                ["👎", "🤬", "💩"],
                ["👎", "🤮"],
                ["💩", "🤬"],
                ["👎"],
            ]
            _rpool = _react_pools[wave_num % len(_react_pools)]
            _react_every = random.randint(2, 4)  # реагируем на каждый 2-4й пост
            _react_count = 0
            _max_browse = min(20, len(msgs))

            for _bi, _bm in enumerate(msgs[:_max_browse]):
                if not (_bm and _bm.id):
                    continue

                # 1. View increment — один пост, как в реальном клиенте при прокрутке
                if _GMV is not None:
                    try:
                        await _timed(
                            client(_GMV(peer=entity, id=[_bm.id], increment=True)), 10.0
                        )
                    except Exception as _ve:
                        if "FLOOD_WAIT" in str(_ve).upper():
                            await asyncio.sleep(_flood(str(_ve), 5))

                # 2. Задержка "чтения" поста (имитирует время просмотра контента)
                await asyncio.sleep(random.betavariate(2, 3) * 2.5 + 0.8)  # 0.8–3.3с

                # 3. Негативная реакция на каждый N-й пост (после "прочтения")
                if negative_react and (_bi % _react_every == 0) and _react_count < 12:
                    try:
                        await client(
                            SendReactionRequest(
                                peer=entity,
                                msg_id=_bm.id,
                                reaction=[
                                    ReactionEmoji(
                                        emoticon=_rpool[_react_count % len(_rpool)]
                                    )
                                ],
                            )
                        )
                        R["reactions_sent"] += 1
                        _react_count += 1
                        await asyncio.sleep(random.uniform(1.0, 3.0))
                    except Exception as _react_err:
                        log.debug(
                            "rpv2[6/react] acc=%s: %s", acc_id, str(_react_err)[:60]
                        )

            # Сохранить 1-2 поста в "Избранное" (поведение пользователя, собирающего доказательства)
            _save_cands = [
                m for m in msgs[:6] if m and not getattr(m, "service", False)
            ][:2]
            for _sv in _save_cands:
                try:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await client.forward_messages("me", _sv)
                except Exception as e:
                    log.debug("rpv2[6/save_to_fav]: %s", e)

            log.info(
                "rpv2[6/browse] viewed=%d reacted=%d saved=%d acc=%s",
                min(_max_browse, len(msgs)),
                R["reactions_sent"],
                len(_save_cands),
                acc_id,
            )

        # ── 7. ReportPeer (все причины) — ПОСЛЕ просмотра контента ───
        # Только теперь пользователь "видел" канал и нажимает "Пожаловаться".
        # Telegram регистрирует: join + views + reactions → report = сильный сигнал.
        reasons_to_send = all_reasons if multi_reason else [tg_reason]
        for idx, r_obj in enumerate(reasons_to_send):
            if idx > 0:
                await asyncio.sleep(random.betavariate(2, 5) * 2.0 + 0.5)
            try:
                await client(
                    ReportPeerRequest(
                        peer=entity,
                        reason=r_obj,
                        message=msg_pool[idx % len(msg_pool)],
                    )
                )
                if idx == 0:
                    R["peer_reported"] = True
                else:
                    R["multi_reason_sent"] += 1
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err.upper():
                    await asyncio.sleep(_flood(err) + random.uniform(1, 3))
                    try:
                        await client(
                            ReportPeerRequest(
                                peer=entity,
                                reason=r_obj,
                                message=msg_pool[idx % len(msg_pool)],
                            )
                        )
                        if idx == 0:
                            R["peer_reported"] = True
                        else:
                            R["multi_reason_sent"] += 1
                    except Exception as e:
                        log.debug("rpv2[7/peer retry] acc=%s: %s", acc_id, e)
                elif "REPORT_TOO_MUCH" in err.upper() or "too_many" in err.lower():
                    _record_error("peer", err)
                    break
                else:
                    _record_error("peer", err)
                    if _abort_signal():
                        break
                    log.warning(
                        "rpv2[7/peer idx=%d] acc=%s: %s", idx, acc_id, err[:100]
                    )
        log.info(
            "rpv2[7] peer=%s multi=%d acc=%s",
            R["peer_reported"],
            R["multi_reason_sent"],
            acc_id,
        )
        await asyncio.sleep(random.uniform(0.8, 2.0))

        # ── 8. Фото профиля ───────────────────────────────────────────
        # Abort all remaining write vectors if the account hit PEER_FLOOD / ban —
        # continuing to report after that signal escalates to a hard ban.
        if report_photo and _RPP and not _abort_signal():
            try:
                photos = await _timed(client.get_profile_photos(entity, limit=1), 10.0)
                if photos:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    _p = photos[0]
                    from telethon.tl.types import InputPhoto as _InputPhoto

                    _photo_input = _InputPhoto(
                        id=_p.id,
                        access_hash=_p.access_hash,
                        file_reference=_p.file_reference,
                    )
                    await client(
                        _RPP(
                            peer=entity,
                            photo_id=_photo_input,
                            reason=tg_reason,
                            message=msg_pool[0],
                        )
                    )
                    R["photo_reported"] = True
                    log.info("rpv2[8/photo] reported acc=%s", acc_id)
            except Exception as e:
                log.warning("rpv2[8/photo] acc=%s: %s", acc_id, str(e)[:80])

        # ── 9. Pinned messages ────────────────────────────────────────
        if report_pinned and is_channel and not _abort_signal():
            try:
                from telethon.tl.types import InputPeerChannel as _IPC5

                _ipeer5 = _IPC5(channel_id=entity.id, access_hash=entity.access_hash)
                pinned = await _timed(
                    client.get_messages(
                        _ipeer5, filter=InputMessagesFilterPinned(), limit=25
                    ),
                    15.0,
                )
                pinned_ids = [m.id for m in pinned if m and m.id]
                log.info("rpv2[9/pin] pinned=%d acc=%s", len(pinned_ids), acc_id)
                for ip, pid in enumerate(pinned_ids):
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 2.0 + 0.5)
                        ok = await _report_message_ids(
                            entity, [pid], msg_pool[ip % len(msg_pool)], f"pin:{pid}"
                        )
                        if ok:
                            R["pinned_reported"] += 1
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_flood(err, 15))
                        elif "REPORT_TOO_MUCH" in err.upper():
                            break
                        else:
                            _record_error("pin", err)
                            if _abort_signal():
                                break
                            log.warning(
                                "rpv2[9/pin] acc=%s pid=%d: %s", acc_id, pid, err[:80]
                            )
            except Exception as e:
                log.warning("rpv2[9/get_pinned] acc=%s: %s", acc_id, str(e)[:80])

        # ── 10. Message chunk reports (MsgReport BFS) ─────────────────
        if msgs and is_channel and _ipeer6 is not None and not _abort_signal():
            msg_ids = [m.id for m in msgs if m and m.id]
            chunks = [msg_ids[i : i + 5] for i in range(0, len(msg_ids), 5)]
            random.shuffle(chunks)
            for ci, chunk in enumerate(chunks):
                cmsg = msg_pool[ci % len(msg_pool)]
                try:
                    ok = await _report_message_ids(
                        _ipeer6, chunk, cmsg, f"msg_chunk:{ci}"
                    )
                    if ok:
                        R["msg_reported"] += len(chunk)
                except Exception as e:
                    err = str(e)
                    if "FLOOD_WAIT" in err.upper():
                        await asyncio.sleep(_flood(err, 15))
                        try:
                            ok = await _report_message_ids(
                                _ipeer6, chunk[:2], cmsg, f"msg_retry:{ci}"
                            )
                            if ok:
                                R["msg_reported"] += min(2, len(chunk))
                        except Exception as e:
                            log.debug("rpv2[10/retry ci=%d] acc=%s: %s", ci, acc_id, e)
                    elif "REPORT_TOO_MUCH" in err.upper():
                        log.info("rpv2[10] REPORT_TOO_MUCH ci=%d, stopping", ci)
                        break
                    else:
                        _record_error("msg_chunk", err)
                        if _abort_signal():
                            break
                        log.warning(
                            "rpv2[10/chunk ci=%d] acc=%s: %s", ci, acc_id, err[:100]
                        )
                await asyncio.sleep(random.betavariate(2, 5) * 2.0 + 0.5)
            log.info("rpv2[10] msg_reported=%d acc=%s", R["msg_reported"], acc_id)

        # ── 11. channels.ReportSpam ────────────────────────────────────
        if _CSR and msgs and is_channel and not _abort_signal():
            spam_ids = [m.id for m in msgs[:15] if m and m.id]
            _spam_participant = None
            for _sm in msgs[:10]:
                _fid = getattr(_sm, "from_id", None)
                if _fid is not None:
                    try:
                        _spam_participant = await _timed(client.get_entity(_fid), 8.0)
                        break
                    except Exception as e:
                        log.debug("rpv2[11/spam_participant] acc=%s: %s", acc_id, e)
            if spam_ids and _spam_participant is None:
                try:
                    _adm_resp = await _timed(
                        client(
                            GetParticipantsRequest(
                                channel=entity,
                                filter=ChannelParticipantsAdmins(),
                                offset=0,
                                limit=10,
                                hash=0,
                            )
                        )
                    )
                    for _au in getattr(_adm_resp, "users", []):
                        if not getattr(_au, "bot", False) and not getattr(
                            _au, "deleted", False
                        ):
                            _spam_participant = _au
                            break
                except Exception as e:
                    log.debug("rpv2[11/spam_admin_fallback] acc=%s: %s", acc_id, e)
            if spam_ids and _spam_participant:
                try:
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    await client(
                        _CSR(channel=entity, participant=_spam_participant, id=spam_ids)
                    )
                    R["spam_signaled"] += len(spam_ids)
                    log.info("rpv2[11] spam_signaled=%d acc=%s", len(spam_ids), acc_id)
                except Exception as e:
                    log.warning("rpv2[11/spam] acc=%s: %s", acc_id, str(e)[:80])

        # ── 12. Report admins ─────────────────────────────────────────
        if report_admins and is_channel and not _abort_signal():
            try:
                adm = await _timed(
                    client(
                        GetParticipantsRequest(
                            channel=entity,
                            filter=ChannelParticipantsAdmins(),
                            offset=0,
                            limit=50,
                            hash=0,
                        )
                    )
                )
                admins = list(getattr(adm, "users", []))
                random.shuffle(admins)
                log.info(
                    "rpv2[12] admins=%d target=%s acc=%s", len(admins), peer, acc_id
                )
                for ai, usr in enumerate(admins):
                    try:
                        await asyncio.sleep(random.betavariate(2, 4) * 1.5 + 0.5)
                        await client(
                            ReportPeerRequest(
                                peer=usr,
                                reason=all_reasons[ai % len(all_reasons)],
                                message=msg_pool[ai % len(msg_pool)],
                            )
                        )
                        R["admins_reported"] += 1
                    except Exception as e:
                        err = str(e)
                        if "FLOOD_WAIT" in err.upper():
                            await asyncio.sleep(_flood(err, 10))
                        else:
                            log.warning(
                                "rpv2[12/admin ai=%d] acc=%s: %s", ai, acc_id, err[:80]
                            )
            except Exception as e:
                log.warning("rpv2[12/get_admins] acc=%s: %s", acc_id, str(e)[:80])

        # ── 13. Linked discussion group ───────────────────────────────
        if report_linked_group and full_chat and not _abort_signal():
            linked_id = getattr(full_chat, "linked_chat_id", None)
            if linked_id:
                try:
                    lent = await _timed(client.get_entity(int(linked_id)), 10.0)
                    for li in range(min(4, len(all_reasons))):
                        try:
                            await asyncio.sleep(random.betavariate(2, 5) * 1.5 + 0.5)
                            await client(
                                ReportPeerRequest(
                                    peer=lent,
                                    reason=all_reasons[li],
                                    message=msg_pool[li % len(msg_pool)],
                                )
                            )
                            R["linked_group_reported"] = True
                        except Exception as e:
                            log.warning(
                                "rpv2[13/linked li=%d] acc=%s: %s",
                                li,
                                acc_id,
                                str(e)[:80],
                            )
                except Exception as e:
                    log.warning("rpv2[13/get_linked] acc=%s: %s", acc_id, str(e)[:80])

        # ── 11. Linked bots ────────────────────────────────────────────
        # ── 14. Linked bots (report) ──────────────────────────────────
        _service_bots = {"stopca", "notoscam", "spambot", "spam_bot", "officialscambot"}
        if report_linked_bots and is_channel and not _abort_signal():
            bot_re = _re.compile(r"@([A-Za-z]\w{4,31}[Bb]ot)\b")
            scan = (getattr(full_chat, "about", "") or "") if full_chat else ""
            for m in msgs[:10]:
                if m and m.text:
                    scan += " " + m.text
            _bot_candidates = [
                b for b in set(bot_re.findall(scan)) if b.lower() not in _service_bots
            ]
            for bi, bname in enumerate(_bot_candidates[:6]):
                try:
                    bent = await _timed(client.get_entity(bname), 8.0)
                    await client(
                        ReportPeerRequest(
                            peer=bent,
                            reason=all_reasons[bi % len(all_reasons)],
                            message=msg_pool[bi % len(msg_pool)],
                        )
                    )
                    R["bots_reported"] += 1
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                except Exception as e:
                    log.warning(
                        "rpv2[14/bot %s] acc=%s: %s", bname, acc_id, str(e)[:80]
                    )

        # ── 15. Forward evidence → anti-abuse bots ────────────────────
        if forward_to_bot and msgs and not _abort_signal():
            target_bots = _fwd_bots_multi.get(reason, ["notoscam"])
            fwd_msgs = [m for m in msgs[:6] if m and not m.service]
            for bot_uname in target_bots:
                try:
                    fbot = await _timed(client.get_entity(bot_uname), 8.0)
                    try:
                        await client.send_message(fbot, "/start")
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                    except Exception as e:
                        log.debug("rpv2[15/fwd_start %s]: %s", bot_uname, e)
                    for em in fwd_msgs[:4]:
                        try:
                            await client.forward_messages(fbot, em)
                            R["forwarded"] += 1
                            await asyncio.sleep(random.uniform(0.8, 1.8))
                        except Exception as e:
                            log.warning(
                                "rpv2[15/fwd %s] acc=%s: %s",
                                bot_uname,
                                acc_id,
                                str(e)[:60],
                            )
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                except Exception as e:
                    log.warning(
                        "rpv2[15/fbot %s] acc=%s: %s", bot_uname, acc_id, str(e)[:80]
                    )

        # ── 16. ReadHistory + Mute + Leave + Block ────────────────────
        # ReadHistory — финальный сигнал "дочитал всё до конца".
        # В связке с per-post view increments создаёт полный поведенческий паттерн.
        if R["joined"] and is_channel and msgs:
            try:
                from telethon.tl.functions.channels import ReadHistoryRequest as _RHR

                _last_id = max((m.id for m in msgs if m and m.id), default=0)
                if _last_id:
                    await _timed(client(_RHR(channel=entity, max_id=_last_id)), 10.0)
            except Exception as e:
                log.debug("rpv2[16/ReadHistory] acc=%s: %s", acc_id, e)
        try:
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

            await client(
                UpdateNotifySettingsRequest(
                    peer=InputNotifyPeer(peer=entity),
                    settings=InputPeerNotifySettings(
                        mute_until=_TELEGRAM_MAX_MUTE_UNTIL
                    ),
                )
            )
        except Exception as e:
            log.debug("rpv2[16/mute] acc=%s: %s", acc_id, e)
        if R["joined"]:
            try:
                await client(LeaveChannelRequest(entity))
            except Exception as e:
                log.debug("rpv2[16/leave] acc=%s: %s", acc_id, e)
        if block_after:
            try:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                await client(BlockRequest(id=entity))
                R["blocked"] = True
            except Exception as e:
                log.warning("rpv2[13/block] acc=%s: %s", acc_id, str(e)[:80])

        log.info(
            "rpv2 DONE acc=%s target=%s | peer=%s msgs=%d pinned=%d admins=%d "
            "views+reacted=%d/%d spam=%d joined=%s",
            acc_id,
            peer,
            R["peer_reported"],
            R["msg_reported"],
            R["pinned_reported"],
            R["admins_reported"],
            R["reactions_sent"],
            R["msgs_fetched"],
            R["spam_signaled"],
            R["joined"],
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("rpv2 FATAL acc=%s target=%s: %s", acc_id, peer, e)
        R["_fatal_error"] = str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("rpv2: disconnect error: %s", e)
    return R


async def strike_map_target(
    session_string: str,
    peer_username: str,
    _acc: dict | None = None,
) -> dict:
    """Разведка перед атакой: полная карта цели за одно подключение.

    Возвращает:
      channel_id, title, description, members, access_hash,
      admin_ids[], linked_group_id, pinned_msg_ids[], latest_msg_ids[],
      mentioned_usernames[], bot_usernames[], error
    """
    import re as _re
    from telethon.tl.functions.channels import (
        GetFullChannelRequest,
        GetParticipantsRequest,
    )
    from telethon.tl.types import (
        Channel,
        ChannelParticipantsAdmins,
        InputMessagesFilterPinned,
    )

    intel: dict = {
        "channel_id": 0,
        "title": "",
        "description": "",
        "members": 0,
        "access_hash": 0,
        "admin_ids": [],
        "linked_group_id": None,
        "pinned_msg_ids": [],
        "latest_msg_ids": [],
        "mentioned_usernames": [],
        "bot_usernames": [],
        "error": None,
    }

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        entity = await client.get_entity(peer_username.lstrip("@"))
        if not isinstance(entity, Channel):
            intel["error"] = "not_a_channel"
            return intel

        intel["channel_id"] = entity.id
        intel["title"] = getattr(entity, "title", "") or ""
        intel["access_hash"] = getattr(entity, "access_hash", 0) or 0
        intel["members"] = getattr(entity, "participants_count", 0) or 0

        # Полная инфо о канале (описание, linked_chat_id) + refresh entity
        try:
            full = await client(GetFullChannelRequest(entity))
            fc = full.full_chat
            intel["description"] = (getattr(fc, "about", "") or "")[:500]
            intel["linked_group_id"] = getattr(fc, "linked_chat_id", None)
            # Refresh entity из ответа (более актуальный access_hash)
            _fc = next(
                (
                    c
                    for c in getattr(full, "chats", [])
                    if getattr(c, "id", None) == entity.id
                ),
                None,
            )
            if _fc:
                entity = _fc
                intel["access_hash"] = getattr(entity, "access_hash", 0) or 0
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Все администраторы (до 200)
        try:
            adm = await client(
                GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsAdmins(),
                    offset=0,
                    limit=200,
                    hash=0,
                )
            )
            intel["admin_ids"] = [u.id for u in getattr(adm, "users", [])]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Закреплённые сообщения
        try:
            pinned = await client.get_messages(
                entity, filter=InputMessagesFilterPinned(), limit=20
            )
            intel["pinned_msg_ids"] = [m.id for m in pinned if m and m.id]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Последние 100 сообщений
        try:
            msgs = await client.get_messages(entity, limit=100)
            intel["latest_msg_ids"] = [m.id for m in msgs if m and m.id]
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        # Упомянутые @usernames и @botы из описания + последних постов
        scan_text = intel["description"]
        try:
            msgs_text = await client.get_messages(entity, limit=15)
            for m in msgs_text:
                if m and m.text:
                    scan_text += " " + m.text
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
        _bot_re = _re.compile(r"@([A-Za-z]\w{3,31}[Bb]ot)\b")
        _chan_re = _re.compile(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,31})\b")
        _at_re = _re.compile(r"@([A-Za-z][A-Za-z0-9_]{3,31})\b")
        intel["bot_usernames"] = list(set(_bot_re.findall(scan_text)))[:8]
        intel["mentioned_usernames"] = list(
            {
                m
                for m in _at_re.findall(scan_text)
                if m.lower() not in {"stopca", "notoscam", "spambot"}
            }
        )[:10]
        # t.me/... ссылки
        intel["mentioned_usernames"] += [
            u
            for u in _chan_re.findall(scan_text)
            if u not in intel["mentioned_usernames"]
        ][:5]

    except Exception as e:
        intel["error"] = str(e)[:200]
        log.warning("strike_map_target error for %s: %s", peer_username, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в strike_map_target")
    return intel


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT PROFILE
# ══════════════════════════════════════════════════════════════════════════════


async def update_profile(
    session_string: str,
    first_name: str | None = None,
    last_name: str | None = None,
    about: str | None = None,
    _acc: dict | None = None,
) -> bool:
    """Update the connected account's profile. Pass None to leave a field unchanged."""
    from telethon.tl.functions.account import UpdateProfileRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client.get_me()
        kwargs: dict = {}
        if first_name is not None:
            kwargs["first_name"] = first_name
        if last_name is not None:
            kwargs["last_name"] = last_name
        if about is not None:
            kwargs["about"] = about
        if not kwargs:
            return True
        await client(UpdateProfileRequest(**kwargs))
        return True
    except Exception as e:
        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            raise
        log.exception("update_profile error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в update_profile")


async def check_username_available(
    session_string: str, username: str, _acc: dict | None = None
) -> bool:
    """Check if a Telegram username is available before attempting to claim it.

    Returns True if available, False if taken or check failed.
    Uses ResolveUsername — if the username resolves, it is taken.
    """
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.errors import UsernameNotOccupiedError, UsernameInvalidError

    clean = username.lstrip("@").strip()
    if not clean or len(clean) < 5 or len(clean) > 32:
        return False  # invalid format, treat as unavailable

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        try:
            await client(ResolveUsernameRequest(username=clean))
            return False  # resolved successfully → username is taken
        except UsernameNotOccupiedError:
            return True  # username is available
        except UsernameInvalidError:
            return False  # invalid username format
        except Exception as e:
            log.warning("is_channel_available inner error: %s", e)
            return False  # unknown error → treat as unavailable to be safe
    except Exception as e:
        log.warning("is_channel_available error: %s", e)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception as e:
            log.debug("check_username_available: disconnect error: %s", e)


async def update_account_username(
    session_string: str, username: str, _acc: dict | None = None
) -> str:
    """Update account username. Returns '' on success, error string on failure."""
    from telethon.tl.functions.account import UpdateUsernameRequest

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)
        await client(UpdateUsernameRequest(username=username.lstrip("@")))
        return ""
    except Exception as e:
        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            return f"FloodWait {e.seconds}с — подождите перед изменением username"
        log.exception("update_account_username error: %s", e)
        return str(e)[:200]
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в update_account_username")


# ══════════════════════════════════════════════════════════════════════════════
# BOTFATHER BOT CREATION
# ══════════════════════════════════════════════════════════════════════════════

_BOTFATHER_USERNAME = "BotFather"

# Phrases BotFather uses at each step of /newbot (English + Russian variants)
_BF_STEP_NAME = (
    "name",
    "alright",
    "good name",
    "few words",
    "how are you going",
    "название",
    "имя",
    "назовите",
    "хорошо",
    "отлично",
)
_BF_STEP_USERNAME = (
    "username",
    "юзернейм",
    "пользователь",
    "логин",
    "choose a username",
    "must end in",
    "должен заканчиваться",
    "choose",
)
_BF_STEP_SUCCESS = (
    "congratulations",
    "done!",
    "t.me/",
    "token",
    "use this token",
    "поздравляем",
    "готово",
    "используйте",
)
_BF_RATE_LIMIT = (
    "too many",
    "try again",
    "slow down",
    "attempts",
    "подождите",
    "попробуй",
)
_BF_USERNAME_TAKEN = ("already", "taken", "занят", "sorry", "exists")


async def create_bot_via_botfather(
    session_string: str,
    bot_display_name: str,
    bot_username: str,
    _acc: dict | None = None,
) -> dict:
    """Create a new Telegram bot via @BotFather automated dialog.

    Returns dict with 'token' and 'username' on success,
    or 'error' key with message on failure.
    """
    import re

    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        # Resolve BotFather entity once so get_messages sender check works
        bf_entity = await client.get_entity(_BOTFATHER_USERNAME)
        bf_id = bf_entity.id

        async def _get_last_bf_msg_id() -> int:
            """Return the message_id of the latest BotFather message (0 if none)."""
            try:
                msgs = await client.get_messages(bf_entity, limit=1)
                if msgs and msgs[0].sender_id == bf_id:
                    return msgs[0].id
            except Exception as e:
                log.debug("create_bot_via_botfather: _get_last_bf_msg_id: %s", e)
            return 0

        async def _bf_send(text: str, timeout: float = 45.0) -> str:
            """Send message to BotFather, wait for and return its response.

            Polls until a NEW message from BotFather appears (message_id > baseline).
            Retries up to 5 times with 3-second intervals before giving up.
            """
            # Record baseline before sending so we detect the NEW response
            baseline_id = await _get_last_bf_msg_id()
            await asyncio.sleep(random.uniform(1.5, 3.5))  # human-like pre-send pause
            await client.send_message(bf_entity, text)

            # Poll for BotFather's reply
            deadline = asyncio.get_event_loop().time() + timeout
            poll_interval = 3.0
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(poll_interval)
                try:
                    msgs = await client.get_messages(bf_entity, limit=3)
                    for msg in msgs:
                        if msg.id > baseline_id and msg.sender_id == bf_id:
                            return msg.text or ""
                except Exception as e:
                    log.debug("create_bot_via_botfather: poll error: %s", e)
                poll_interval = min(poll_interval + 1.0, 8.0)  # back off slowly
            return ""  # timed out

        def _parse_flood_wait(text: str) -> int | None:
            m = re.search(r"try again in (\d+) seconds?", text, re.IGNORECASE)
            if m:
                return int(m.group(1))
            m = re.search(r"(\d+)\s*(?:сек|секунд)", text, re.IGNORECASE)
            return int(m.group(1)) if m else None

        async def _bf_send_with_retry(text: str, max_retries: int = 2) -> str:
            for attempt in range(max_retries + 1):
                resp = await _bf_send(text)
                wait = _parse_flood_wait(resp)
                if wait is None:
                    return resp
                jitter = random.randint(10, 30)
                total_wait = wait + jitter
                log.info("BotFather rate limit: waiting %ds", total_wait)
                if attempt == max_retries:
                    return resp
                await asyncio.sleep(total_wait)
            return ""

        async def _bf_cancel() -> None:
            """Cancel any in-progress BotFather dialog."""
            try:
                await client.send_message(bf_entity, "/cancel")
                await asyncio.sleep(random.uniform(2.0, 4.0))
            except Exception as e:
                log.debug("create_bot_via_botfather: _bf_cancel: %s", e)

        # Validate username format before starting dialog
        uname = bot_username.lstrip("@").strip()
        if not uname.lower().endswith("bot"):
            uname = uname + "_bot"
        if len(uname) < 5 or len(uname) > 32:
            return {
                "error": f"Username @{uname} слишком короткий или длинный (5-32 символа)"
            }

        # Step 1: /newbot — may land in an incomplete previous flow
        resp = await _bf_send_with_retry("/newbot")
        resp_low = resp.lower()

        wait = _parse_flood_wait(resp)
        if wait is not None:
            return {
                "error": f"BotFather: слишком много попыток, подождите {wait}с",
                "flood_wait": wait,
            }

        # Detect incomplete previous flow (BotFather asks for username without asking for name first)
        if any(k in resp_low for k in _BF_STEP_USERNAME) and not any(
            k in resp_low for k in _BF_STEP_NAME
        ):
            await _bf_cancel()
            resp = await _bf_send_with_retry("/newbot")
            resp_low = resp.lower()
            wait = _parse_flood_wait(resp)
            if wait is not None:
                return {
                    "error": f"BotFather: слишком много попыток, подождите {wait}с",
                    "flood_wait": wait,
                }

        if not resp or not any(k in resp_low for k in _BF_STEP_NAME):
            await _bf_cancel()
            return {"error": f"Неожиданный ответ BotFather на /newbot: {resp[:200]}"}

        # Step 2: send display name
        resp = await _bf_send_with_retry(bot_display_name)
        resp_low = resp.lower()
        if not resp or not any(k in resp_low for k in _BF_STEP_USERNAME):
            wait = _parse_flood_wait(resp)
            if wait is not None:
                await _bf_cancel()
                return {
                    "error": f"BotFather rate limit после имени: {wait}с",
                    "flood_wait": wait,
                }
            await _bf_cancel()
            return {"error": f"Неожиданный ответ после имени бота: {resp[:200]}"}

        # Step 3: send username — retry with variants if taken
        from services.username_engine import generate_username_variants, unique_bot_username

        uname_variants = [uname]
        for v in generate_username_variants(uname):
            if v not in uname_variants and v.endswith("bot") or not v.endswith("bot"):
                # For bots, prefer bot-suffixed variants but include all
                uname_variants.append(v if v.endswith("bot") else f"{v}bot")
        # Add short-suffix variants
        for i in range(20):
            candidate = unique_bot_username(uname, i)
            if candidate not in uname_variants:
                uname_variants.append(candidate)

        actual_uname = None
        resp = ""
        for attempt_uname in uname_variants[:15]:
            if not (5 <= len(attempt_uname) <= 32):
                continue
            resp = await _bf_send_with_retry(attempt_uname)
            resp_low = resp.lower()
            if any(k in resp_low for k in _BF_STEP_SUCCESS):
                actual_uname = attempt_uname
                break
            if any(k in resp_low for k in _BF_USERNAME_TAKEN) and not any(
                k in resp_low for k in _BF_STEP_SUCCESS
            ):
                # BotFather stays in username step — just send next variant
                await asyncio.sleep(random.uniform(1.5, 3.0))
                continue
            # Non-taken error or rate limit — stop
            break

        if actual_uname is None:
            await _bf_cancel()
            return {"error": f"Не удалось найти свободный username (попробовано {min(len(uname_variants), 15)}). Последний ответ: {resp[:100]}"}

        # Check for rate limit
        wait = _parse_flood_wait(resp)
        if wait is not None:
            await _bf_cancel()
            return {
                "error": f"BotFather rate limit при создании: {wait}с",
                "flood_wait": wait,
            }

        # Extract token
        token_match = re.search(r"\b(\d{8,12}:[A-Za-z0-9_-]{35,})\b", resp)
        if not token_match:
            await _bf_cancel()
            return {"error": f"Токен не найден в ответе BotFather: {resp[:300]}"}

        token = token_match.group(1)
        return {
            "token": token,
            "username": actual_uname,
            "display_name": bot_display_name,
        }

    except Exception as e:
        from telethon.errors import FloodWaitError

        if isinstance(e, FloodWaitError):
            return {
                "error": f"FloodWait {e.seconds}с — Telegram ограничил создание",
                "flood_wait": e.seconds,
            }
        log.exception("create_bot_via_botfather error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в create_bot_via_botfather")


async def list_bots_via_botfather(
    session_string: str,
    _acc: dict | None = None,
) -> dict:
    """List bots owned by this account via @BotFather /mybots.

    Returns {"bots": [{"username": "..."}]} or {"error": "..."}.
    """
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        bf_entity = await client.get_entity(_BOTFATHER_USERNAME)
        bf_id = bf_entity.id

        msgs = await client.get_messages(bf_entity, limit=1)
        baseline_id = msgs[0].id if (msgs and msgs[0].sender_id == bf_id) else 0

        await asyncio.sleep(random.uniform(1.5, 3.5))
        await client.send_message(bf_entity, "/mybots")

        deadline = asyncio.get_event_loop().time() + 30.0
        response_msg = None
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3.0)
            try:
                msgs = await client.get_messages(bf_entity, limit=5)
                for msg in msgs:
                    if msg.id > baseline_id and msg.sender_id == bf_id:
                        response_msg = msg
                        break
            except Exception as e:
                log.debug("list_bots_via_botfather: poll error: %s", e)
            if response_msg is not None:
                break

        if response_msg is None:
            return {"error": "Timeout: BotFather did not respond to /mybots"}

        text = (response_msg.text or "").lower()
        if "don't have any bots" in text or "у вас нет ботов" in text or "no bots" in text:
            return {"bots": []}

        bots: list[dict] = []
        seen: set[str] = set()

        if response_msg.buttons:
            for row in response_msg.buttons:
                for btn in row:
                    btn_text = getattr(btn, "text", "") or ""
                    if btn_text.startswith("@"):
                        uname = btn_text.lstrip("@").strip()
                        if uname and uname not in seen:
                            seen.add(uname)
                            bots.append({"username": uname})

        if not bots:
            for match in re.finditer(r"@([\w]{3,}bot)", response_msg.text or "", re.IGNORECASE):
                uname = match.group(1)
                if uname not in seen:
                    seen.add(uname)
                    bots.append({"username": uname})

        return {"bots": bots}

    except Exception as e:
        log.exception("list_bots_via_botfather error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в list_bots_via_botfather")


async def transfer_bot_via_botfather(
    session_string: str,
    bot_username: str,
    new_owner_username: str,
    _acc: dict | None = None,
) -> dict:
    """Transfer bot ownership to another user via @BotFather dialog.

    Returns {"ok": True, "message": text} or {"error": "..."}.
    """
    client = _make_client(session_string, _acc)
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT)

        bf_entity = await client.get_entity(_BOTFATHER_USERNAME)
        bf_id = bf_entity.id

        async def _get_last_bf_msg_id() -> int:
            try:
                msgs = await client.get_messages(bf_entity, limit=1)
                if msgs and msgs[0].sender_id == bf_id:
                    return msgs[0].id
            except Exception as e:
                log.debug("transfer_bot_via_botfather: _get_last_bf_msg_id: %s", e)
            return 0

        async def _bf_wait_for_new(baseline_id: int, timeout: float = 30.0):
            """Poll BotFather chat until a new message (id > baseline) appears."""
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(3.0)
                try:
                    msgs = await client.get_messages(bf_entity, limit=5)
                    for msg in msgs:
                        if msg.id > baseline_id and msg.sender_id == bf_id:
                            return msg
                except Exception as e:
                    log.debug("transfer_bot_via_botfather: poll error: %s", e)
            return None

        async def _bf_send(text: str, timeout: float = 45.0):
            baseline_id = await _get_last_bf_msg_id()
            await asyncio.sleep(random.uniform(1.5, 3.5))
            await client.send_message(bf_entity, text)
            return await _bf_wait_for_new(baseline_id, timeout)

        async def _click_button(msg, text_to_match: str):
            """Click the first button whose text contains text_to_match (case-insensitive)."""
            match_low = text_to_match.lower()
            if not msg.buttons:
                return None
            for r_idx, row in enumerate(msg.buttons):
                for c_idx, btn in enumerate(row):
                    btn_text = (getattr(btn, "text", "") or "").lower()
                    if match_low in btn_text:
                        baseline_id = await _get_last_bf_msg_id()
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                        try:
                            await msg.click(r_idx, c_idx)
                        except Exception as e:
                            log.debug("transfer_bot_via_botfather: msg.click fallback: %s", e)
                            await btn.click()
                        return await _bf_wait_for_new(baseline_id)
            return None

        bot_uname = bot_username.lstrip("@").strip()
        new_owner = new_owner_username.lstrip("@").strip()

        mybots_msg = await _bf_send("/mybots")
        if mybots_msg is None:
            return {"error": "Timeout: BotFather did not respond to /mybots"}

        settings_msg = await _click_button(mybots_msg, f"@{bot_uname}")
        if settings_msg is None:
            settings_msg = await _click_button(mybots_msg, bot_uname)
        if settings_msg is None:
            return {"error": f"Bot @{bot_uname} not found in BotFather button list"}

        await asyncio.sleep(random.uniform(1.5, 3.5))

        transfer_msg = await _click_button(settings_msg, "transfer")
        if transfer_msg is None:
            transfer_msg = await _click_button(settings_msg, "передать")
        if transfer_msg is None:
            return {"error": "Transfer Ownership button not found in bot settings"}

        await asyncio.sleep(random.uniform(1.5, 3.5))

        confirm_msg = await _bf_send(f"@{new_owner}")
        if confirm_msg is None:
            return {"error": "Timeout: BotFather did not ask for confirmation"}

        confirm_text = (confirm_msg.text or "").lower()

        if "are you sure" in confirm_text or "вы уверены" in confirm_text or "уверен" in confirm_text:
            await asyncio.sleep(random.uniform(1.5, 3.5))
            final_msg = await _bf_send("Yes, I am sure.")
            if final_msg is None:
                return {"error": "Timeout: BotFather did not respond after confirmation"}
            confirm_text = (final_msg.text or "").lower()
            final_text = final_msg.text or ""
        else:
            final_text = confirm_msg.text or ""

        success_indicators = ("transfer", "success", "done", "transferred", "передан", "передача", "успешно")
        if any(ind in confirm_text for ind in success_indicators):
            return {"ok": True, "message": final_text}

        return {"error": f"Unexpected BotFather response: {final_text[:300]}"}

    except Exception as e:
        log.exception("transfer_bot_via_botfather error: %s", e)
        return {"error": str(e)[:200]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой в transfer_bot_via_botfather")


async def validate_session_import(session_str: str, proxy_url: str | None = None) -> dict:
    try:
        client = _make_client(session_str)
        await asyncio.wait_for(client.connect(), timeout=15)
        me = await client.get_me()
        await client.disconnect()
        return {"valid": True, "phone": me.phone or "", "user_id": me.id}
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


def detect_session_format(data: str) -> str:
    data = data.strip()
    if data.startswith('{'):
        try:
            import json
            parsed = json.loads(data)
            if 'dc_id' in parsed and 'api_id' in parsed:
                return 'pyrogram_json'
        except Exception as e:
            log.debug("detect_session_format: json parse: %s", e)
    if len(data) > 100 and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in data):
        return 'string_session'
    return 'unknown'
