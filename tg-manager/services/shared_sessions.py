"""Общее подключение аккаунта: одна живая сессия Telegram на аккаунт в процессе.

ЗАЧЕМ. До этого модуля каждое действие открывало СВОЁ подключение
(`_make_client(...).connect()` → действие → `disconnect()`), а процессный
мьютекс сессии (`account_manager._session_inuse`) пускал к аккаунту ровно
одно подключение. Отсюда два следствия, на которые жаловался владелец:

  * операции на одном аккаунте шли строго по очереди: вторая получала
    `SessionBusyError` и ждала, пока первая отключится;
  * аккаунт «мигал»: connect → одно действие → disconnect десятки раз в час.
    Живой человек так не делает — он держит одно подключение и совмещает
    несколько дел, и именно это Telegram видит как нормальное поведение.

ЧТО ДЕЛАЕТ. Когда режим включён (по умолчанию — да, см. ВКЛЮЧЕНИЕ) клиент,
который отдаёт `account_manager._make_client` / `connect_client`, — это
лёгкий держатель (`SharedClient`). Его `connect()` берёт ОБЩЕЕ подключение
аккаунта (создаёт его при первом обращении штатным `connect_client`, со всеми
фолбэками и ретраями), а `disconnect()` только отпускает свою ссылку. Само
подключение закрывается после простоя `FLEET_SHARED_IDLE_SEC` без держателей.

ПОЧЕМУ ЭТО НЕ РИСК AUTH_KEY_DUPLICATED. Инвариант «одна сессия — одно живое
подключение» сохраняется буквально: подключение по-прежнему одно и держит тот
же мьютекс сессии. Меняется только то, что через него теперь могут идти
запросы нескольких операций одновременно — MTProto это умеет (Telethon
мультиплексирует запросы одного клиента).

ТЕМП. Когда подключение делят несколько держателей, между запросами аккаунта
ставится зазор: короткий для чтения и длинный для «пишущих» действий
(вступить, пригласить, отправить...). Нижний порог флуд-паузы
(`_wrap_client_flood_floor`) работает как раньше — он стоит на самом клиенте.

ВКЛЮЧЕНИЕ. По умолчанию ВКЛЮЧЕНО для всех. Окружение `FLEET_SHARED_SESSIONS`
(1/0) перекрывает всё; без него решает режим в базе (`platform_settings.fleet_shared_sessions`: off | all | owners:1,2) —
его ставит админ командой /fleet_parallel без деплоя, в том числе на одного
владельца для канарейки.

ОТКАТ. `/fleet_parallel off` (и снять переменную окружения, если ставили) → `_make_client` и `connect_client` ведут себя ровно как до
этого модуля (каждый вызов — своё подключение).
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import os
import random
import time
import weakref
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


# ── Настройки ────────────────────────────────────────────────────────────────

def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


_forced: "bool | None" = None

# Режим из базы (platform_settings, ключ SETTING_KEY) — включение без деплоя и
# ПО ВЛАДЕЛЬЦАМ (канарейка): "off" | "all" | "owners:1,2,3". Переключает
# админ командой /fleet_parallel; каждый процесс перечитывает значение раз в
# REFRESH_S (refresh_from_db зовёт цикл исполнителя), так что web и worker
# сходятся за полминуты.
SETTING_KEY = "fleet_shared_sessions"
REFRESH_S = 30.0
# ПО УМОЛЧАНИЮ ВКЛЮЧЕНО (решение владельца 21.11.2026): нет записи в базе —
# операции делят аккаунты. Выключает только явное "off" (/fleet_parallel off)
# или окружение FLEET_SHARED_SESSIONS=0.
DEFAULT_MODE = "all"
_db_mode: str = DEFAULT_MODE
_db_owners: frozenset = frozenset()
_db_read_at: float = 0.0

# Владелец операции, в задаче которой идёт код (ставит op_worker). По нему режим
# «owners:…» решает, делить ли подключение: словарь аккаунта владельца знает не
# на каждом пути.
_current_owner: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "shared_sessions_owner", default=None)


def parse_mode(raw: "str | None") -> "tuple[str, frozenset]":
    """Разбор значения настройки. Нет значения — режим по умолчанию (вкл);
    непонятное значение — выключено (осторожнее при опечатке админа)."""
    v = str(raw or "").strip().lower()
    if not v:
        return parse_mode(DEFAULT_MODE) if DEFAULT_MODE else ("off", frozenset())
    if v in ("all", "on", "1", "true"):
        return "all", frozenset()
    if v.startswith("owners:"):
        ids = set()
        for part in v[len("owners:"):].split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                ids.add(int(part))
        return ("owners", frozenset(ids)) if ids else ("off", frozenset())
    return "off", frozenset()


def format_mode(mode: str, owners) -> str:
    if mode == "owners":
        return "owners:" + ",".join(str(i) for i in sorted(owners))
    return mode


def set_mode(raw: "str | None") -> None:
    """Применить значение настройки в этом процессе (после записи в базу)."""
    global _db_mode, _db_owners, _db_read_at
    _db_mode, _db_owners = parse_mode(raw)
    _db_read_at = time.monotonic()


def current_mode() -> "tuple[str, frozenset]":
    if _forced is not None:
        return ("all" if _forced else "off"), frozenset()
    env = _env_value()
    if env is not None:
        return ("all" if env else "off"), frozenset()
    return _db_mode, _db_owners


async def refresh_from_db(pool, *, force_read: bool = False) -> None:
    """Перечитать режим из базы не чаще REFRESH_S. Сбой чтения — оставляем
    прежнее значение: блип базы не должен ни включать, ни выключать режим."""
    if pool is None:
        return
    if not force_read and (time.monotonic() - _db_read_at) < REFRESH_S:
        return
    try:
        raw = await pool.fetchval(
            "SELECT value FROM platform_settings WHERE key = $1", SETTING_KEY)
    except Exception as e:
        log.debug("shared_sessions: режим не прочитан: %s", e)
        return
    set_mode(raw)


async def save_mode(pool, raw: str) -> "tuple[str, frozenset]":
    """Записать режим в базу и применить в этом процессе."""
    mode, owners = parse_mode(raw)
    value = format_mode(mode, owners)
    await pool.execute(
        """INSERT INTO platform_settings (key, value, updated_at)
                VALUES ($1, $2, now())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()""",
        SETTING_KEY, value)
    set_mode(value)
    return mode, owners


def _env_value() -> "bool | None":
    """FLEET_SHARED_SESSIONS: 1 — включить всем, 0 — выключить всем (аварийный
    рубильник поверх базы), пусто — решает настройка в базе."""
    v = str(os.getenv("FLEET_SHARED_SESSIONS", "")).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def enabled(owner_id: "int | None" = None) -> bool:
    """Включено ли общее подключение для этого владельца.

    Владелец — явный аргумент, иначе владелец операции из контекста задачи.
    Без владельца в режиме «owners:…» — выключено (старый путь безопасен
    всегда: он просто не делит подключение)."""
    mode, owners = current_mode()
    if mode == "all":
        return True
    if mode != "owners":
        return False
    oid = owner_id if owner_id is not None else _current_owner.get()
    try:
        return oid is not None and int(oid) in owners
    except (TypeError, ValueError):
        return False


def force(value: "bool | None") -> None:
    """Принудительно включить/выключить (None — снова читать окружение и базу)."""
    global _forced
    _forced = value


def idle_close_s() -> float:
    return _env_float("FLEET_SHARED_IDLE_SEC", 120.0, 5.0, 3600.0)


def read_gap_s() -> float:
    return _env_float("FLEET_SHARED_MIN_GAP_SEC", 0.4, 0.0, 30.0)


def write_gap_s() -> float:
    return _env_float("FLEET_SHARED_WRITE_GAP_SEC", 2.5, 0.0, 120.0)


# ── Метрики ──────────────────────────────────────────────────────────────────

def _metric(name: str, value: float = 1.0, **labels) -> None:
    try:
        from services import metrics as _m
        _m.inc(name, {k: str(v) for k, v in labels.items() if v is not None}, value)
    except Exception:
        pass


# ── Классификация запросов для темпа ─────────────────────────────────────────

# Префиксы имён TL-запросов, которые МЕНЯЮТ что-то в Telegram. Именно на них
# антиспам смотрит строже всего, поэтому зазор у них длиннее.
_WRITE_PREFIXES = (
    "Send", "Forward", "Invite", "Join", "Import", "Leave", "Delete", "Edit",
    "Report", "Create", "Update", "Upload", "Set", "Toggle", "Block", "Unblock",
    "Add", "Save", "Install", "Start", "Vote", "Migrate", "Transfer", "Hide",
    "Promote", "Kick", "Ban", "Accept", "Reject",
)
# Служебные запросы, которые не являются действиями аккаунта и не должны
# задерживаться (пинги, состояние обновлений, загрузка частей файла).
_EXEMPT = frozenset({
    "PingRequest", "PingDelayDisconnectRequest", "GetStateRequest",
    "GetDifferenceRequest", "GetChannelDifferenceRequest", "SaveFilePartRequest",
    "SaveBigFilePartRequest", "GetFileRequest", "UpdateStatusRequest",
    "InvokeWithLayerRequest", "InitConnectionRequest", "GetConfigRequest",
})


def classify(names: list[str]) -> str:
    """'exempt' | 'write' | 'read' для пачки имён запросов."""
    names = [n for n in names if n]
    if not names or all(n in _EXEMPT for n in names):
        return "exempt"
    for n in names:
        if n in _EXEMPT:
            continue
        if n.startswith(_WRITE_PREFIXES):
            return "write"
    return "read"


# Ошибки, после которых подключение больше никому не отдаём: сессия мертва
# или её видят с двух мест. Следующий держатель получит новое подключение.
_POISON_ERRORS = frozenset({
    "AuthKeyUnregisteredError", "AuthKeyDuplicatedError", "AuthKeyInvalidError",
    "SessionRevokedError", "SessionExpiredError", "UserDeactivatedError",
    "UserDeactivatedBanError", "AuthKeyError", "UnauthorizedError",
})


# ── Хаб ──────────────────────────────────────────────────────────────────────

class _Entry:
    __slots__ = ("key", "account_id", "client", "refs", "lock", "idle_handle",
                 "poisoned", "pace_lock", "last_read", "last_write", "loop",
                 "created", "__weakref__")

    def __init__(self, key: str, account_id: "int | None", loop) -> None:
        self.key = key
        self.account_id = account_id
        self.client: Any = None
        self.refs = 0
        self.lock = asyncio.Lock()
        self.idle_handle: "asyncio.TimerHandle | None" = None
        self.poisoned = False
        self.pace_lock = asyncio.Lock()
        self.last_read = 0.0
        self.last_write = 0.0
        self.loop = loop
        self.created = time.monotonic()


# (id цикла событий, ключ сессии) → запись. Ключ с циклом: тесты и
# вспомогательные потоки поднимают свои циклы, и клиент одного цикла в другом
# не работает.
_entries: dict[tuple[int, str], _Entry] = {}


def _entry_key(key: str) -> tuple[int, str]:
    return (id(asyncio.get_running_loop()), key)


def _client_connected(client) -> bool:
    try:
        r = client.is_connected()
        if inspect.isawaitable(r):  # pragma: no cover - telethon отдаёт bool
            try:
                r.close()
            except Exception:
                pass
            return True
        return bool(r)
    except Exception:
        return False


async def _close_client(client) -> None:
    if client is None:
        return
    try:
        res = client.disconnect()
        if inspect.isawaitable(res):
            await asyncio.wait_for(res, timeout=10.0)
    except Exception:
        log.debug("shared_sessions: отключение не удалось", exc_info=True)


def _install_pacer(entry: _Entry, client) -> None:
    """Зазор между запросами аккаунта, пока подключение делят несколько дел.

    Ставится поверх `_call` (там уже стоит нижний порог флуд-паузы), поэтому
    действует на любой запрос любого держателя. Пока держатель один — ничего
    не меняется: темп одиночной операции задаёт сама операция, как раньше.
    """
    orig = getattr(client, "_call", None)
    if orig is None or getattr(client, "_infragram_shared_pacer", False):
        return
    ref = weakref.ref(entry)

    async def _call(sender, request, *a, **k):
        ent = ref()
        items = request if isinstance(request, (list, tuple)) else [request]
        names = [type(r).__name__ for r in items if r is not None]
        kind = classify(names)
        if ent is not None and ent.refs > 1 and kind != "exempt":
            async with ent.pace_lock:
                now = time.monotonic()
                if kind == "write":
                    gap = write_gap_s()
                    last = max(ent.last_write, ent.last_read)
                else:
                    gap = read_gap_s()
                    last = ent.last_read
                if gap > 0:
                    # Ровный интервал — тоже подпись бота: добавляем разброс.
                    gap *= random.uniform(1.0, 1.4)
                wait = (last + gap) - now
                if wait > 0:
                    _metric("infragram_shared_session_paced_total", 1, kind=kind)
                    await asyncio.sleep(wait)
                now = time.monotonic()
                ent.last_read = now
                if kind == "write":
                    ent.last_write = now
        try:
            return await orig(sender, request, *a, **k)
        except Exception as e:
            if ent is not None and type(e).__name__ in _POISON_ERRORS:
                ent.poisoned = True
            raise

    try:
        client._call = _call
        client._infragram_shared_pacer = True
    except Exception:
        log.debug("shared_sessions: клиент не принял темп", exc_info=True)


def _schedule_idle_close(entry: _Entry) -> None:
    if entry.idle_handle is not None:
        entry.idle_handle.cancel()
        entry.idle_handle = None

    def _fire() -> None:
        entry.idle_handle = None
        if entry.refs > 0:
            return
        asyncio.ensure_future(_close_entry(entry, "idle"))

    try:
        entry.idle_handle = entry.loop.call_later(idle_close_s(), _fire)
    except Exception:
        asyncio.ensure_future(_close_entry(entry, "idle"))


async def _close_entry(entry: _Entry, reason: str) -> None:
    async with entry.lock:
        if entry.refs > 0 or entry.client is None:
            return
        client, entry.client = entry.client, None
        _entries.pop((id(entry.loop), entry.key), None)
    _metric("infragram_shared_session_closes_total", 1, reason=reason)
    await _close_client(client)


async def acquire(key: str, account_id: "int | None",
                  connect: Callable[[], Awaitable[Any]]) -> _Entry:
    """Взять ссылку на общее подключение (создать, если его нет)."""
    ek = _entry_key(key)
    while True:
        entry = _entries.get(ek)
        if entry is None or entry.poisoned:
            if entry is not None and entry.refs == 0:
                # Отравленное и никем не держимое — закрыть до нового коннекта,
                # иначе новый упрётся в мьютекс сессии старого.
                await _close_entry(entry, "poisoned")
            if entry is not None and entry.poisoned and entry.refs > 0:
                # Его ещё держат — второго подключения к той же сессии не
                # открываем; подождём, пока отпустят.
                await asyncio.sleep(0.5)
                continue
            entry = _Entry(key, account_id, asyncio.get_running_loop())
            _entries[ek] = entry
        async with entry.lock:
            if _entries.get(ek) is not entry:
                continue  # запись закрыли, пока ждали замок
            if entry.client is not None and not _client_connected(entry.client):
                stale, entry.client = entry.client, None
                _metric("infragram_shared_session_closes_total", 1, reason="dropped")
                await _close_client(stale)
            if entry.client is None:
                try:
                    client = await connect()
                except BaseException:
                    if entry.refs == 0:
                        _entries.pop(ek, None)
                    raise
                _install_pacer(entry, client)
                entry.client = client
                entry.poisoned = False
                _metric("infragram_shared_session_acquires_total", 1, result="connected")
            else:
                _metric("infragram_shared_session_acquires_total", 1, result="reused")
            entry.refs += 1
            if entry.idle_handle is not None:
                entry.idle_handle.cancel()
                entry.idle_handle = None
            return entry


def release(entry: _Entry) -> None:
    """Отпустить ссылку. Синхронно: зовётся и из финализатора держателя."""
    if entry.refs > 0:
        entry.refs -= 1
    if entry.refs > 0:
        return
    if entry.poisoned or entry.client is None or not _client_connected(entry.client):
        try:
            entry.loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(_close_entry(
                    entry, "poisoned" if entry.poisoned else "dead")))
        except RuntimeError:
            pass  # цикл уже закрыт — закрывать нечего
        return
    try:
        if entry.loop.is_closed():
            return
        entry.loop.call_soon_threadsafe(_schedule_idle_close, entry)
    except RuntimeError:
        pass


def holders(key: str) -> int:
    """Сколько держателей у общего подключения (0 — нет подключения)."""
    try:
        e = _entries.get(_entry_key(key))
    except RuntimeError:
        return 0
    return e.refs if e is not None and e.client is not None else 0


def stats() -> dict:
    """Снимок для экрана «Здоровье» и логов."""
    out = {"connections": 0, "holders": 0, "shared": 0}
    for e in list(_entries.values()):
        if e.client is None:
            continue
        out["connections"] += 1
        out["holders"] += e.refs
        if e.refs > 1:
            out["shared"] += 1
    return out


async def close_all() -> int:
    """Закрыть все общие подключения (остановка процесса, тесты)."""
    n = 0
    for e in list(_entries.values()):
        if e.idle_handle is not None:
            e.idle_handle.cancel()
            e.idle_handle = None
        client, e.client = e.client, None
        e.refs = 0
        if client is not None:
            n += 1
            await _close_client(client)
    _entries.clear()
    return n


# ── Держатель ────────────────────────────────────────────────────────────────

_OWN = frozenset({
    "_sc_key", "_sc_account_id", "_sc_connect", "_sc_detached_factory",
    "_sc_entry", "_sc_detached", "_sc_pending", "_sc_handlers", "_sc_finalizer",
})


class SharedClient:
    """То, что получает вызывающий код вместо TelegramClient.

    Ведёт себя как клиент Telethon: всё, кроме connect/disconnect/is_connected
    и регистрации обработчиков, проксируется в общее подключение. Если код
    обращается к клиенту ДО connect() (например, читает `client.session`),
    держатель уходит в «отдельный» режим — строит обычный клиент, ровно как
    до этого модуля, и дальше работает только с ним.
    """

    def __init__(self, key: str, account_id: "int | None",
                 connect: Callable[[], Awaitable[Any]],
                 detached_factory: Callable[[], Any]) -> None:
        object.__setattr__(self, "_sc_key", key)
        object.__setattr__(self, "_sc_account_id", account_id)
        object.__setattr__(self, "_sc_connect", connect)
        object.__setattr__(self, "_sc_detached_factory", detached_factory)
        object.__setattr__(self, "_sc_entry", None)
        object.__setattr__(self, "_sc_detached", None)
        object.__setattr__(self, "_sc_pending", {})
        object.__setattr__(self, "_sc_handlers", [])
        object.__setattr__(self, "_sc_finalizer", None)

    # -- внутреннее --
    def _sc_target(self):
        entry = object.__getattribute__(self, "_sc_entry")
        if entry is not None and entry.client is not None:
            return entry.client
        det = object.__getattribute__(self, "_sc_detached")
        if det is None:
            det = object.__getattribute__(self, "_sc_detached_factory")()
            for k, v in object.__getattribute__(self, "_sc_pending").items():
                try:
                    setattr(det, k, v)
                except Exception:
                    pass
            object.__setattr__(self, "_sc_detached", det)
            _metric("infragram_shared_session_detached_total", 1)
        return det

    # -- жизненный цикл --
    async def connect(self, *a, **k):
        det = object.__getattribute__(self, "_sc_detached")
        if det is not None:
            r = det.connect(*a, **k)
            return (await r) if inspect.isawaitable(r) else r
        cur = object.__getattribute__(self, "_sc_entry")
        if cur is not None:
            if cur.client is not None and _client_connected(cur.client):
                return None
            # Подключение под держателем умерло (сеть, прокси) — повторный
            # connect() вызывающего означает «переподключись»: отпускаем
            # мёртвое и берём живое заново.
            await self.disconnect()
        entry = await acquire(object.__getattribute__(self, "_sc_key"),
                              object.__getattribute__(self, "_sc_account_id"),
                              object.__getattribute__(self, "_sc_connect"))
        object.__setattr__(self, "_sc_entry", entry)
        pending = object.__getattribute__(self, "_sc_pending")
        for k_, v_ in list(pending.items()):
            try:
                setattr(entry.client, k_, v_)
            except Exception:
                pass
        pending.clear()
        # Не отключился явно (забыли finally) — ссылка вернётся при сборке
        # мусора, а не будет держать подключение вечно.
        object.__setattr__(self, "_sc_finalizer", weakref.finalize(self, release, entry))
        return None

    async def disconnect(self, *a, **k):
        det = object.__getattribute__(self, "_sc_detached")
        if det is not None:
            r = det.disconnect(*a, **k)
            return (await r) if inspect.isawaitable(r) else r
        entry = object.__getattribute__(self, "_sc_entry")
        if entry is None:
            return None
        object.__setattr__(self, "_sc_entry", None)
        handlers = object.__getattribute__(self, "_sc_handlers")
        if entry.client is not None:
            for cb, ev in handlers:
                try:
                    entry.client.remove_event_handler(cb, ev)
                except Exception:
                    pass
        handlers.clear()
        fin = object.__getattribute__(self, "_sc_finalizer")
        if fin is not None:
            fin.detach()
            object.__setattr__(self, "_sc_finalizer", None)
        release(entry)
        return None

    def is_connected(self) -> bool:
        det = object.__getattribute__(self, "_sc_detached")
        if det is not None:
            return _client_connected(det)
        entry = object.__getattribute__(self, "_sc_entry")
        return bool(entry is not None and entry.client is not None
                    and _client_connected(entry.client))

    async def start(self, *a, **k):
        if a or k:
            # start() с телефоном/паролем — это вход, а не работа аккаунтом.
            det = SharedClient._sc_target(self)
            r = det.start(*a, **k)
            return (await r) if inspect.isawaitable(r) else r
        await self.connect()
        return self

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    # -- обработчики событий: снимаются при disconnect держателя --
    def add_event_handler(self, callback, event=None):
        target = SharedClient._sc_target(self)
        target.add_event_handler(callback, event)
        if object.__getattribute__(self, "_sc_detached") is None:
            object.__getattribute__(self, "_sc_handlers").append((callback, event))

    def on(self, event):
        def deco(f):
            self.add_event_handler(f, event)
            return f
        return deco

    # -- прокси --
    def __getattr__(self, name: str) -> Any:
        if name in _OWN:
            raise AttributeError(name)
        return getattr(SharedClient._sc_target(self), name)

    def __setattr__(self, name: str, value: Any) -> None:
        entry = object.__getattribute__(self, "_sc_entry")
        det = object.__getattribute__(self, "_sc_detached")
        if entry is None and det is None:
            object.__getattribute__(self, "_sc_pending")[name] = value
            return
        setattr(SharedClient._sc_target(self), name, value)

    def __call__(self, *a, **k):
        return SharedClient._sc_target(self)(*a, **k)

    def __repr__(self) -> str:  # pragma: no cover - для логов
        return (f"<SharedClient acc={object.__getattribute__(self, '_sc_account_id')} "
                f"connected={self.is_connected()}>")
