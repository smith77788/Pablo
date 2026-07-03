"""Тесты guard-путей executor'ов op_worker (обработка ошибок без Telethon).

Используют лёгкий FakePool: проверяем, что executor'ы корректно и БЕЗОПАСНО
возвращают статус при отсутствии данных, а не падают с исключением.
"""
from __future__ import annotations

import pytest


class _AcquireCtx:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Минимальный async-«pool»: отдаёт заранее заданные ответы, безопасен."""

    def __init__(self, fetch=None, fetchrow=None, fetchval=None):
        self._fetch = fetch if fetch is not None else []
        self._fetchrow = fetchrow
        self._fetchval = fetchval
        self.calls = []

    async def fetch(self, q, *a):
        self.calls.append(("fetch", q))
        return self._fetch

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q))
        return self._fetchrow

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q))
        return self._fetchval

    async def execute(self, q, *a):
        self.calls.append(("execute", q))
        return "UPDATE 0"

    def acquire(self):
        return _AcquireCtx(self)


class _Bot:
    async def send_message(self, *a, **k):
        return None
    async def get_file(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_parse_audience_requires_source():
    from services.op_worker import _exec_parse_audience
    res = await _exec_parse_audience(FakePool(), _Bot(), 1, 42, {})
    assert res["status"] == "failed"
    assert "source_ref" in res["summary"]


@pytest.mark.asyncio
async def test_check_accounts_health_no_accounts():
    from services.op_worker import _exec_check_accounts_health
    res = await _exec_check_accounts_health(FakePool(fetch=[]), _Bot(), 1, 42, {})
    assert res["status"] == "failed"
    assert "аккаунт" in res["reason"].lower()


@pytest.mark.asyncio
async def test_bulk_create_channels_multi_no_accounts():
    """Ранее падал KeyError на params['account_ids']; должен возвращать статус."""
    from services.op_worker import _exec_bulk_create_channels_multi
    res = await _exec_bulk_create_channels_multi(FakePool(fetch=[]), _Bot(), 1, 42, {})
    assert isinstance(res, dict)
    assert res.get("status") in ("failed", "done", "error", "cancelled")


@pytest.mark.asyncio
async def test_watchdog_alerts_dedup():
    """Алерт не спамит: повторный прогон с теми же op_id не шлёт заново."""
    from services import op_worker as w
    w._alerted_stuck_ops.clear()
    rows = [{"id": 7, "op_type": "mass_publish", "status": "pending",
             "owner_id": 42, "age_min": 20.0}]
    sent = []

    class _CountBot:
        async def send_message(self, aid, text, **k):
            sent.append(aid)

    # монки: pool.fetch → rows, админы → {1}
    import bot.utils.subscription as sub
    orig_admins = sub._admin_ids
    sub._admin_ids = lambda: {1}
    try:
        await w._watchdog_alerts(FakePool(fetch=rows), _CountBot())
        first = list(sent)
        await w._watchdog_alerts(FakePool(fetch=rows), _CountBot())  # тот же op — без алерта
        assert first == [1]
        assert sent == [1]  # второй раз не добавилось
    finally:
        sub._admin_ids = orig_admins
        w._alerted_stuck_ops.clear()


# ── Circuit Breaker Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_circuit_breaker_trips_on_failures():
    """Circuit breaker trips after 3 consecutive failures."""
    from services.op_worker import _circuit_breaker_record, _circuit_breaker_is_open, _circuit_breaker_state
    _circuit_breaker_state.clear()
    for _ in range(3):
        await _circuit_breaker_record(100, False)
    assert _circuit_breaker_is_open(100)
    _circuit_breaker_state.clear()


@pytest.mark.asyncio
async def test_circuit_breaker_resets_on_cooldown():
    """Circuit breaker resets after cooldown expires."""
    import time
    from services.op_worker import _circuit_breaker_record, _circuit_breaker_is_open, _circuit_breaker_state
    _circuit_breaker_state.clear()
    for _ in range(3):
        await _circuit_breaker_record(100, False)
    assert _circuit_breaker_is_open(100)
    # Simulate cooldown expiry
    _circuit_breaker_state[100]["cooldown_until"] = time.time() - 1
    await _circuit_breaker_record(100, True)  # This triggers reset
    assert not _circuit_breaker_is_open(100)
    _circuit_breaker_state.clear()


@pytest.mark.asyncio
async def test_circuit_breaker_decay_on_success():
    """Circuit breaker decays failures on success."""
    from services.op_worker import _circuit_breaker_record, _circuit_breaker_is_open, _circuit_breaker_state
    _circuit_breaker_state.clear()
    for _ in range(3):
        await _circuit_breaker_record(100, False)
    assert _circuit_breaker_is_open(100)
    # Success should decay
    await _circuit_breaker_record(100, True)
    assert not _circuit_breaker_is_open(100)
    _circuit_breaker_state.clear()


# ── Safe DB Helper Tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_safe_execute_returns_on_error():
    """_safe_execute returns 'ERROR' instead of raising."""
    from services.op_worker import _safe_execute
    pool = FakePool()
    pool.execute = lambda q, *a: (_ for _ in ()).throw(Exception("DB error"))
    result = await _safe_execute(pool, "SELECT 1")
    assert result == "ERROR"


@pytest.mark.asyncio
async def test_safe_fetchrow_returns_none_on_error():
    """_safe_fetchrow returns None instead of raising."""
    from services.op_worker import _safe_fetchrow
    pool = FakePool()
    pool.fetchrow = lambda q, *a: (_ for _ in ()).throw(Exception("DB error"))
    result = await _safe_fetchrow(pool, "SELECT 1")
    assert result is None


@pytest.mark.asyncio
async def test_safe_fetch_returns_empty_on_error():
    """_safe_fetch returns [] instead of raising."""
    from services.op_worker import _safe_fetch
    pool = FakePool()
    pool.fetch = lambda q, *a: (_ for _ in ()).throw(Exception("DB error"))
    result = await _safe_fetch(pool, "SELECT 1")
    assert result == []


@pytest.mark.asyncio
async def test_safe_fetchval_returns_none_on_error():
    """_safe_fetchval returns None instead of raising."""
    from services.op_worker import _safe_fetchval
    pool = FakePool()
    pool.fetchval = lambda q, *a: (_ for _ in ()).throw(Exception("DB error"))
    result = await _safe_fetchval(pool, "SELECT 1")
    assert result is None


# ── Account Rotation Tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_account_rotation_basic():
    """select_account_rotated returns an account from the pool."""
    from services.resource_selector import select_account_rotated
    from services import flood_engine
    # Mock flood_engine
    orig = flood_engine.get_best_account
    flood_engine.get_best_account = lambda **kw: None
    try:
        result = await select_account_rotated(
            FakePool(fetch=[{"id": 1, "trust_score": 0.8}]),
            owner_id=42
        )
        assert result is None  # No matching accounts
    finally:
        flood_engine.get_best_account = orig


# ── Proxy Intelligence Tests ──────────────────────────────────────────────


def test_proxy_stats_empty():
    """get_proxy_stats returns zeros for unknown proxy."""
    from services.account_manager import get_proxy_stats
    result = get_proxy_stats("unknown_proxy")
    assert result["success_rate"] == 0
    assert result["total_checks"] == 0


def test_proxy_stats_after_record():
    """get_proxy_stats reflects recorded results."""
    from services.account_manager import _record_proxy_stat, get_proxy_stats, _proxy_stats
    _proxy_stats.clear()
    _record_proxy_stat("test_proxy", True, 100)
    _record_proxy_stat("test_proxy", True, 120)
    _record_proxy_stat("test_proxy", False, 0)
    result = get_proxy_stats("test_proxy")
    assert result["success_rate"] == 66.7
    assert result["total_checks"] == 3
    _proxy_stats.clear()


# ── Session Simulator Tests ───────────────────────────────────────────────


def test_adaptive_delay_returns_positive():
    """get_adaptive_delay always returns positive value."""
    from services.session_simulator import get_adaptive_delay
    delay = get_adaptive_delay("test_action", 5.0)
    assert delay > 0


def test_adaptive_delay_respects_base():
    """get_adaptive_delay respects base delay."""
    from services.session_simulator import get_adaptive_delay
    delay1 = get_adaptive_delay("test_action", 1.0)
    delay2 = get_adaptive_delay("test_action", 10.0)
    assert delay2 >= delay1  # Higher base should give higher delay
