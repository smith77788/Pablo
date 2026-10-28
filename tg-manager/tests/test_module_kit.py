"""module_kit — единый каркас: гейт → постановка → событие."""
from __future__ import annotations

import asyncio

import pytest

from services import module_kit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_submit_guarded_gate_blocks(monkeypatch):
    async def not_ready(pool, owner, op):
        return False, "Давление 90/100"
    monkeypatch.setattr(module_kit, "ready_for", not_ready)
    with pytest.raises(module_kit.OpGateError) as ei:
        _run(module_kit.submit_guarded(None, 1, "bulk_dm_adhoc", {}, label="x"))
    assert "Давление" in str(ei.value)


def test_submit_guarded_happy_path(monkeypatch):
    async def ready(pool, owner, op):
        return True, ""
    submitted = {}

    async def fake_submit(pool, owner, op_type, params, **kw):
        submitted.update({"op_type": op_type, "kw": kw})
        return 777

    monkeypatch.setattr(module_kit, "ready_for", ready)
    import services.operation_bus as ob
    monkeypatch.setattr(ob, "submit", fake_submit)

    op_id = _run(module_kit.submit_guarded(None, 1, "bulk_join",
                                           {"links": ["@x"]}, total_items=5, label="L"))
    assert op_id == 777
    assert submitted["op_type"] == "bulk_join" and submitted["kw"]["total_items"] == 5
    # событие op_queued эмитит сам operation_bus (единый choke point), не module_kit


def test_gate_false_skips_check(monkeypatch):
    called = {"ready": False}
    async def ready(pool, owner, op):
        called["ready"] = True
        return True, ""
    async def fake_submit(pool, owner, op_type, params, **kw):
        return 1
    monkeypatch.setattr(module_kit, "ready_for", ready)
    monkeypatch.setattr(module_kit, "emit", lambda *a, **k: _noop())
    import services.operation_bus as ob
    monkeypatch.setattr(ob, "submit", fake_submit)
    _run(module_kit.submit_guarded(None, 1, "x", {}, gate=False))
    assert called["ready"] is False   # гейт пропущен


async def _noop():
    return None


def test_operation_bus_emits_op_queued_centrally():
    # Единый choke point: любая операция любого модуля → событие op_queued.
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "operation_bus.py"), encoding="utf-8").read()
    # Якорь — единственный INSERT ... RETURNING id в submit(); после него (закрытие
    # транзакции, лог, ветка дедупа) обязан идти централизованный emit op_queued.
    tail = src[src.rindex("RETURNING id"):]
    assert "spine" in tail and '"op_queued"' in tail


def test_ready_for_failopen(monkeypatch):
    # сбой проверки давления не должен блокировать (fail-open → True)
    import services.infra_orchestrator as io
    async def boom(pool, owner, op, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(io, "is_ready_for_op", boom)
    ok, reason = _run(module_kit.ready_for(None, 1, "x"))
    assert ok is True
