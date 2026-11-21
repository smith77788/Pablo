"""Несколько операций одновременно на одном аккаунте флота.

Жалоба владельца: операции шли строго по очереди. Аккаунт держала ровно одна
операция (исключающий захват `_accounts_in_use`), а к сессии пускалось ровно
одно подключение (мьютекс `_session_inuse`), причём каждое действие открывало
своё подключение заново. Вторая операция на том же аккаунте ждала до 20 минут
и падала «все аккаунты заняты».

Под флагом FLEET_SHARED_SESSIONS=1:
  * подключение аккаунта ОДНО на процесс и общее (services/shared_sessions);
  * до OP_ACCOUNT_MAX_SHARED_OPS операций держат аккаунт одновременно;
  * аренда освобождается, только когда отпустила последняя операция;
  * фоновые циклы остаются исключающими;
  * без флага всё как раньше.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import op_worker as ow
from services import shared_sessions as ss


# ── заглушка клиента Telethon ───────────────────────────────────────────────

class _Req:
    pass


class SendMessageRequest(_Req):
    pass


class GetHistoryRequest(_Req):
    pass


class AuthKeyUnregisteredError(Exception):
    pass


class FakeClient:
    def __init__(self):
        self.connected = True
        self.calls: list[tuple[str, float]] = []
        self.handlers: list = []
        self.disconnects = 0
        self.raise_next: "Exception | None" = None

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.disconnects += 1
        self.connected = False

    async def _call(self, sender, request, *a, **k):
        if self.raise_next is not None:
            e, self.raise_next = self.raise_next, None
            raise e
        self.calls.append((type(request).__name__, time.monotonic()))
        return "ok"

    def __call__(self, request):
        return self._call(None, request)

    def add_event_handler(self, cb, ev=None):
        self.handlers.append((cb, ev))

    def remove_event_handler(self, cb, ev=None):
        self.handlers.remove((cb, ev))

    async def get_me(self):
        return "me"


@pytest.fixture(autouse=True)
def _flag_and_cleanup(monkeypatch):
    ss.force(True)
    monkeypatch.setattr(ss, "idle_close_s", lambda: 0.05)
    monkeypatch.setattr(ss, "read_gap_s", lambda: 0.0)
    monkeypatch.setattr(ss, "write_gap_s", lambda: 0.0)
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    ow._op_shares.clear()
    monkeypatch.setattr(ow, "_db_pool", None)
    yield
    ss.force(None)
    ss._entries.clear()
    ow._accounts_in_use.clear()
    ow._operation_account_locks.clear()
    ow._op_shares.clear()


def _handle(made: list, key="s:k1"):
    async def _connect():
        c = FakeClient()
        made.append(c)
        return c
    return ss.SharedClient(key, 1, _connect, lambda: FakeClient())


# ── общее подключение ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_holders_share_one_connection():
    made: list = []
    a, b = _handle(made), _handle(made)
    await asyncio.gather(a.connect(), b.connect())
    assert len(made) == 1, "второй держатель обязан взять то же подключение"
    assert ss.holders("s:k1") == 2
    assert await a.get_me() == "me" and await b.get_me() == "me"
    await a.disconnect()
    assert made[0].connected, "подключение живо, пока его держит второй"
    await b.disconnect()
    await asyncio.sleep(0.2)
    assert not made[0].connected, "после простоя без держателей — закрыто"
    assert ss.holders("s:k1") == 0


@pytest.mark.asyncio
async def test_reuse_within_idle_window_does_not_reconnect():
    made: list = []
    a = _handle(made)
    await a.connect()
    await a.disconnect()
    b = _handle(made)
    await b.connect()              # раньше конца простоя
    assert len(made) == 1
    await b.disconnect()


@pytest.mark.asyncio
async def test_poisoned_connection_is_not_handed_out_again():
    made: list = []
    a = _handle(made)
    await a.connect()
    made[0].raise_next = AuthKeyUnregisteredError()
    with pytest.raises(AuthKeyUnregisteredError):
        await a(SendMessageRequest())
    await a.disconnect()
    await asyncio.sleep(0.05)
    b = _handle(made)
    await b.connect()
    assert len(made) == 2, "мёртвая сессия не отдаётся следующему держателю"
    await b.disconnect()


@pytest.mark.asyncio
async def test_writes_are_spaced_only_when_shared(monkeypatch):
    monkeypatch.setattr(ss, "write_gap_s", lambda: 0.1)
    made: list = []
    a, b = _handle(made), _handle(made)
    await a.connect()
    await a(SendMessageRequest())
    await a(SendMessageRequest())
    t = [c[1] for c in made[0].calls]
    assert t[1] - t[0] < 0.08, "одиночного держателя темп не трогает"
    await b.connect()
    made[0].calls.clear()
    await asyncio.gather(a(SendMessageRequest()), b(SendMessageRequest()))
    t = sorted(c[1] for c in made[0].calls)
    assert t[1] - t[0] >= 0.1, "пишущие запросы двух дел разнесены зазором"
    await a.disconnect()
    await b.disconnect()


@pytest.mark.asyncio
async def test_event_handlers_removed_with_their_holder():
    made: list = []
    a, b = _handle(made), _handle(made)
    await a.connect()
    await b.connect()

    async def cb(ev):
        pass
    a.add_event_handler(cb, "ev")
    assert made[0].handlers == [(cb, "ev")]
    await a.disconnect()
    assert made[0].handlers == [], "обработчик держателя не живёт после него"
    await b.disconnect()


def test_access_before_connect_falls_back_to_own_client():
    made: list = []
    h = _handle(made)
    assert h.is_connected() is False
    # Чтение атрибута до connect — старое поведение: свой клиент.
    assert h.handlers == []
    assert made == []


def test_classify():
    assert ss.classify(["SendMessageRequest"]) == "write"
    assert ss.classify(["InviteToChannelRequest"]) == "write"
    assert ss.classify(["GetHistoryRequest"]) == "read"
    assert ss.classify(["PingRequest"]) == "exempt"


def test_flag_off_by_default(monkeypatch):
    ss.force(None)
    monkeypatch.delenv("FLEET_SHARED_SESSIONS", raising=False)
    assert ss.enabled() is False
    assert ow.shared_ops_mode() is False


def test_make_client_returns_shared_holder_under_flag():
    from services import account_manager as am
    h = am._make_client("plain-session-string", {"id": 7})
    assert isinstance(h, ss.SharedClient)
    ss.force(False)
    h2 = am._make_client("plain-session-string", {"id": 7})
    assert not isinstance(h2, ss.SharedClient), "без флага — как раньше"


# ── совместная аренда аккаунтов операциями ──────────────────────────────────

def _accs(*ids):
    return [{"id": i} for i in ids]


async def _in_op(op_id, coro_fn):
    tok = ow._current_op.set(op_id)
    try:
        return await coro_fn()
    finally:
        ow._current_op.reset(tok)


@pytest.mark.asyncio
async def test_two_ops_take_the_same_accounts_under_flag():
    a = await ow._claim_available_accounts(101, _accs(1, 2), owner_id=5)
    b = await ow._claim_available_accounts(102, _accs(1, 2), owner_id=5)
    assert [x["id"] for x in a] == [1, 2]
    assert [x["id"] for x in b] == [1, 2], (
        "вторая операция обязана получить те же аккаунты, а не ждать первую")
    assert ow._op_shares == {1: 2, 2: 2}
    await ow.release_operation_accounts(101)
    assert ow.is_account_in_use(1), "аккаунт занят, пока его держит вторая"
    await ow.release_operation_accounts(102)
    assert not ow.is_account_in_use(1) and ow._op_shares == {}


@pytest.mark.asyncio
async def test_without_flag_second_op_waits_as_before():
    ss.force(False)
    a = await ow._claim_available_accounts(101, _accs(1), owner_id=5)
    b = await ow._claim_available_accounts(102, _accs(1), owner_id=5)
    assert [x["id"] for x in a] == [1] and b == []


@pytest.mark.asyncio
async def test_share_cap(monkeypatch):
    monkeypatch.setattr(ow, "_MAX_SHARED_OPS", 2)
    await ow._claim_available_accounts(1, _accs(9), owner_id=5)
    await ow._claim_available_accounts(2, _accs(9), owner_id=5)
    c = await ow._claim_available_accounts(3, _accs(9), owner_id=5)
    assert c == [], "больше OP_ACCOUNT_MAX_SHARED_OPS операций на аккаунт нельзя"


@pytest.mark.asyncio
async def test_background_loop_stays_exclusive():
    # Прогрев (без контекста операции) взял аккаунт — операция его не делит.
    assert await ow.try_claim_accounts([4]) == [4]
    got = await ow._claim_available_accounts(201, _accs(4), owner_id=5)
    assert got == []
    # И наоборот: аккаунт операции фоновому циклу не достаётся.
    await ow._claim_available_accounts(202, _accs(6), owner_id=5)
    assert await ow.try_claim_accounts([6]) == []


@pytest.mark.asyncio
async def test_release_in_op_context_drops_only_own_share():
    await ow._claim_available_accounts(301, _accs(8), owner_id=5)
    got = await _in_op(302, lambda: ow.try_claim_accounts([8]))
    assert got == [8]
    await _in_op(301, lambda: ow.release_accounts([8]))
    assert ow.is_account_in_use(8), "release операции A не снимает аккаунт с B"
    assert ow._op_shares[8] == 1
    await _in_op(302, lambda: ow.release_accounts([8]))
    assert not ow.is_account_in_use(8)


@pytest.mark.asyncio
async def test_op_cannot_double_claim_own_account():
    await ow._claim_available_accounts(401, _accs(3), owner_id=5)
    got = await _in_op(401, lambda: ow.try_claim_accounts([3]))
    assert got == [], "повторный захват своего аккаунта — отказ, как без флага"
    assert ow._op_shares[3] == 1


@pytest.mark.asyncio
async def test_reconnect_after_network_drop():
    made: list = []
    a = _handle(made)
    await a.connect()
    made[0].connected = False          # сеть/прокси оборвали подключение
    assert a.is_connected() is False
    await a.connect()                  # вызывающий переподключается
    assert len(made) == 2 and a.is_connected()
    await a.disconnect()


# ── включение без деплоя и по владельцам (/fleet_parallel) ──────────────────

class _SettingsPool:
    def __init__(self, value=None, fail=False):
        self.value, self.fail, self.reads = value, fail, 0

    async def fetchval(self, q, key):
        self.reads += 1
        if self.fail:
            raise RuntimeError("db down")
        assert key == ss.SETTING_KEY
        return self.value

    async def execute(self, q, key, value):
        self.value = value


def test_parse_mode():
    assert ss.parse_mode("all") == ("all", frozenset())
    assert ss.parse_mode("owners:5, 7") == ("owners", frozenset({5, 7}))
    assert ss.parse_mode("owners:") == ("off", frozenset())
    assert ss.parse_mode("мусор") == ("off", frozenset())
    assert ss.parse_mode(None) == ("off", frozenset())


@pytest.mark.asyncio
async def test_canary_owner_only(monkeypatch):
    ss.force(None)
    monkeypatch.delenv("FLEET_SHARED_SESSIONS", raising=False)
    pool = _SettingsPool()
    await ss.save_mode(pool, "owners:42")
    assert pool.value == "owners:42"
    assert ss.enabled(42) is True
    assert ss.enabled(43) is False
    assert ss.enabled() is False, "без владельца в режиме канарейки — старый путь"
    tok = ss._current_owner.set(42)
    try:
        assert ss.enabled() is True and ow.shared_ops_mode() is True
    finally:
        ss._current_owner.reset(tok)
    await ss.save_mode(pool, "off")
    assert ss.enabled(42) is False
    ss.set_mode("off")


@pytest.mark.asyncio
async def test_two_ops_share_only_for_canary_owner(monkeypatch):
    ss.force(None)
    monkeypatch.delenv("FLEET_SHARED_SESSIONS", raising=False)
    ss.set_mode("owners:42")
    try:
        a = await ow._claim_available_accounts(501, _accs(1), owner_id=42)
        b = await ow._claim_available_accounts(502, _accs(1), owner_id=42)
        assert [x["id"] for x in a] == [1] and [x["id"] for x in b] == [1]
        c = await ow._claim_available_accounts(601, _accs(2), owner_id=43)
        d = await ow._claim_available_accounts(602, _accs(2), owner_id=43)
        assert [x["id"] for x in c] == [2] and d == [], "чужой владелец — как раньше"
    finally:
        ss.set_mode("off")


@pytest.mark.asyncio
async def test_db_blip_keeps_mode(monkeypatch):
    ss.force(None)
    monkeypatch.delenv("FLEET_SHARED_SESSIONS", raising=False)
    ss.set_mode("all")
    try:
        await ss.refresh_from_db(_SettingsPool(fail=True), force_read=True)
        assert ss.enabled(1) is True, "сбой чтения базы не выключает режим"
        p = _SettingsPool("off")
        await ss.refresh_from_db(p)          # раньше REFRESH_S — не читает
        assert p.reads == 0
        await ss.refresh_from_db(p, force_read=True)
        assert ss.enabled(1) is False
    finally:
        ss.set_mode("off")


def test_env_still_enables_for_all(monkeypatch):
    ss.force(None)
    ss.set_mode("off")
    monkeypatch.setenv("FLEET_SHARED_SESSIONS", "1")
    assert ss.enabled(999) is True
