"""Единый предохранитель Telethon-вызовов (Стадия 2, #3).

Проверяем контракт: FloodWait → disconnect + handoff (без сна потока), транзиентные
→ ретрай с бэкоффом, критичные → немедленный проброс, неизвестные → критичные.
"""
from __future__ import annotations

import asyncio

import pytest

from services.telethon_guard import (
    FloodHandoff, classify_telethon_error, guarded_call,
)


# ── классификация ─────────────────────────────────────────────────────────────

class FloodWaitError(Exception):
    def __init__(self, seconds): self.seconds = seconds


class AuthKeyUnregisteredError(Exception): ...
class PeerFloodError(Exception): ...
class ServerError(Exception): ...


def test_classify():
    assert classify_telethon_error(FloodWaitError(30)) == "flood"
    assert classify_telethon_error(AuthKeyUnregisteredError()) == "critical"
    assert classify_telethon_error(PeerFloodError()) == "critical"   # спам-блок не ретраим
    assert classify_telethon_error(ServerError()) == "transient"
    assert classify_telethon_error(TimeoutError()) == "transient"
    assert classify_telethon_error(ConnectionError()) == "transient"
    assert classify_telethon_error(ValueError("что-то своё")) == "critical"  # неизвестное → безопасно


# ── стенд клиента ─────────────────────────────────────────────────────────────

class _Client:
    def __init__(self): self.disconnected = 0
    async def disconnect(self): self.disconnected += 1


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _fast(_): return None
    monkeypatch.setattr("services.telethon_guard.asyncio.sleep", _fast)


# ── поведение ─────────────────────────────────────────────────────────────────

def test_success_passthrough():
    c = _Client()
    async def factory(): return "ok"
    assert _run(guarded_call(c, factory)) == "ok"
    assert c.disconnected == 0


def test_flood_disconnects_and_handoffs_without_sleeping():
    c = _Client()
    async def factory(): raise FloodWaitError(120)
    with pytest.raises(FloodHandoff) as ei:
        _run(guarded_call(c, factory))
    assert ei.value.seconds == 120
    assert c.disconnected == 1, "при FloodWait клиент обязан отключиться (не сливать через мёртвый прокси)"


def test_transient_retries_then_succeeds():
    c = _Client()
    calls = {"n": 0}
    async def factory():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServerError()
        return "recovered"
    assert _run(guarded_call(c, factory, retries=3)) == "recovered"
    assert calls["n"] == 3


def test_transient_exhausts_and_propagates():
    c = _Client()
    async def factory(): raise TimeoutError()
    with pytest.raises(TimeoutError):
        _run(guarded_call(c, factory, retries=2))


def test_critical_propagates_immediately_no_retry():
    c = _Client()
    calls = {"n": 0}
    async def factory():
        calls["n"] += 1
        raise AuthKeyUnregisteredError()
    with pytest.raises(AuthKeyUnregisteredError):
        _run(guarded_call(c, factory, retries=3))
    assert calls["n"] == 1, "критичную ошибку не ретраим"


def test_console_paths_use_guard():
    """Предохранитель реально подключён к сетевым вызовам консоли аккаунта."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "services" / "account_console.py").read_text(encoding="utf-8")
    assert src.count("guarded_call(") >= 3, "guarded_call не обёрнут вокруг send/history"
    for anchor in ('action="dm_send"', 'action="dm_file"', 'action="history"'):
        assert anchor in src, f"нет обёртки {anchor}"
