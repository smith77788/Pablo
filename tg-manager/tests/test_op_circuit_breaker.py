"""Полное покрытие предохранителя операций (services/op_circuit_breaker.py)
без зависимости от живого Postgres.

Пуристая логика (переходы, открыт/закрыт, статус) + путь БуД через фейковый пул
(запись под блокировкой строки, чтение общего состояния, fail-open при сбое).
Межпроцессную семантику на реальной БД проверяет test_circuit_breaker_shared_postgres.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from services import op_circuit_breaker as cb


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset():
    cb._circuit_breaker_state.clear()
    cb.set_pool(None)
    yield
    cb._circuit_breaker_state.clear()
    cb.set_pool(None)


# ── Чистая машина состояний ────────────────────────────────────────────────────
def test_apply_trips_after_threshold():
    st = cb._cb_blank()
    for _ in range(cb._CIRCUIT_BREAKER_THRESHOLD):
        cb._cb_apply(st, False, 1000.0)
    assert st["tripped_at"] == 1000.0
    assert st["cooldown_until"] == 1000.0 + cb._CIRCUIT_BREAKER_COOLDOWN
    assert cb._cb_is_open(st, 1000.0) is True


def test_apply_success_decays_and_closes():
    st = {"failures": 5, "tripped_at": 10.0, "cooldown_until": 9999.0}
    cb._cb_apply(st, True, 100.0)      # успех гасит счётчик
    assert st["failures"] == 4
    # ниже порога — цепь закрывается досрочно
    st = {"failures": cb._CIRCUIT_BREAKER_THRESHOLD, "tripped_at": 10.0, "cooldown_until": 9999.0}
    cb._cb_apply(st, True, 100.0)
    assert st["failures"] == cb._CIRCUIT_BREAKER_THRESHOLD - 1
    assert st["tripped_at"] is None and st["cooldown_until"] is None


def test_apply_resets_after_cooldown_expiry():
    st = {"failures": 9, "tripped_at": 1.0, "cooldown_until": 50.0}
    cb._cb_apply(st, False, 100.0)     # cooldown вышел → обнуление, затем +1 сбой
    assert st["failures"] == 1 and st["tripped_at"] is None


def test_is_open_false_paths():
    assert cb._cb_is_open(None, 1.0) is False
    assert cb._cb_is_open({}, 1.0) is False
    # cooldown истёк → закрыта
    assert cb._cb_is_open({"tripped_at": 1.0, "cooldown_until": 5.0}, 100.0) is False


def test_status_open_and_closed():
    assert cb._cb_status(None, 1.0) == {"status": "closed", "failures": 0}
    # закрыта, но был tripped и cooldown истёк → failures обнуляем в статусе
    assert cb._cb_status({"failures": 9, "tripped_at": 1.0, "cooldown_until": 2.0}, 100.0) \
        == {"status": "closed", "failures": 0}
    # открыта → остаток cooldown
    st = {"failures": 3, "tripped_at": 100.0, "cooldown_until": 400.0}
    out = cb._cb_status(st, 100.0)
    assert out["status"] == "open" and out["cooldown_remaining_s"] == 300


def test_from_row_converts_timestamps():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = {"failures": 4, "tripped_at": dt, "cooldown_until": None}
    st = cb._cb_from_row(row)
    assert st["failures"] == 4 and st["tripped_at"] == dt.timestamp()
    assert st["cooldown_until"] is None


# ── Путь без БД (кэш процесса) ─────────────────────────────────────────────────
def test_in_memory_record_trips_and_readers_agree():
    for _ in range(cb._CIRCUIT_BREAKER_THRESHOLD - 1):
        assert _run(cb._circuit_breaker_record(1, False)) is False
    assert _run(cb._circuit_breaker_record(1, False)) is True   # порог → лог TRIPPED
    assert cb._circuit_breaker_is_open(1) is True
    assert cb._circuit_breaker_status(1)["status"] == "open"


def test_shared_readers_fall_back_to_cache_without_pool():
    _run(cb._circuit_breaker_record(2, False))
    # пула нет → circuit_breaker_is_open/status берут кэш процесса
    assert _run(cb.circuit_breaker_is_open(2)) is False   # один сбой < порога
    assert _run(cb.circuit_breaker_status(2))["status"] == "closed"


# ── Путь БД через фейковый пул ─────────────────────────────────────────────────
class _FakeConn:
    def __init__(self, store):
        self.store = store

    def transaction(self):
        conn = self
        class _Tx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return False
        return _Tx()

    async def execute(self, sql, *args):
        if sql.strip().startswith("INSERT"):
            self.store.setdefault(int(args[0]), {"failures": 0, "tripped_at": None,
                                                 "cooldown_until": None})
        elif sql.strip().startswith("UPDATE"):
            oid = int(args[0])
            self.store[oid] = {"failures": int(args[1]),
                               "tripped_at": args[2], "cooldown_until": args[3]}
        return "OK"

    async def fetchrow(self, sql, *args):
        oid = int(args[0])
        rec = self.store.get(oid)
        if rec is None:
            return None
        def _dt(v):
            return None if v is None else datetime.fromtimestamp(v, tz=timezone.utc)
        return {"failures": rec["failures"],
                "tripped_at": _dt(rec["tripped_at"]),
                "cooldown_until": _dt(rec["cooldown_until"])}


class _FakePool:
    def __init__(self):
        self.store: dict = {}

    def acquire(self):
        conn = _FakeConn(self.store)
        class _Ctx:
            async def __aenter__(self):
                return conn
            async def __aexit__(self, *a):
                return False
        return _Ctx()

    async def fetchrow(self, sql, *args):
        return await _FakeConn(self.store).fetchrow(sql, *args)


def test_db_backed_record_and_load_roundtrip():
    cb.set_pool(_FakePool())
    for _ in range(cb._CIRCUIT_BREAKER_THRESHOLD - 1):
        assert _run(cb._circuit_breaker_record(5, False)) is False
    assert _run(cb._circuit_breaker_record(5, False)) is True    # порог в БД
    # свежая реплика (пустой кэш) читает общее состояние и видит паузу
    cb._circuit_breaker_state.clear()
    assert _run(cb.circuit_breaker_is_open(5)) is True
    assert _run(cb.circuit_breaker_status(5))["status"] == "open"


def test_db_record_fail_open_on_pool_error():
    class _BadPool:
        def acquire(self):
            raise RuntimeError("БД отвалилась")
        async def fetchrow(self, *a):
            raise RuntimeError("БД отвалилась")
    cb.set_pool(_BadPool())
    # запись падает на кэш (fail-open), а не запрещает всё
    assert _run(cb._circuit_breaker_record(6, False)) is False
    # чтение общего состояния тоже падает на кэш
    assert _run(cb.circuit_breaker_is_open(6)) is False
