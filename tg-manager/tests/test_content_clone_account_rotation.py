"""Клонер: упёршийся во флуд аккаунт сменяется следующим (регресс).

Было: операция захватывала все выбранные аккаунты, а работала только первым.
Peer flood на первом обрывал всю операцию «аккаунт ограничен», хотя
остальные аккаунты были свободны.
"""
from __future__ import annotations

import asyncio

from services import content_cloner_engine as cce
from services import flood_engine as fe
from services import op_worker as ow


def _run(monkeypatch, behaviour, n_acc=2):
    accs = [{"id": i, "session_str": "s"} for i in range(1, n_acc + 1)]
    calls: list = []
    floods: list = []

    async def _sel(*a, **k):
        return accs

    async def _claim(op_id, a, owner):
        return a

    async def _quar(pool, op_id, a):
        return a, 0

    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return set()

    async def _false(*a, **k):
        return False

    async def _delay(*a, **k):
        return 0

    async def _clone(sess, acc, src, tgt, ids, mode):
        calls.append((acc["id"], tgt))
        return behaviour(acc["id"], tgt)

    async def _rec(pool, acc_id, secs, *a, **k):
        floods.append(acc_id)

    monkeypatch.setattr(ow.resource_selector, "select_all_active", _sel)
    monkeypatch.setattr(ow, "_claim_available_accounts", _claim)
    monkeypatch.setattr(ow, "_filter_quarantined_accounts", _quar)
    monkeypatch.setattr(ow, "completed_targets", _empty)
    monkeypatch.setattr(ow, "_safe_execute", _none)
    monkeypatch.setattr(ow, "_is_cancelled", _false)
    monkeypatch.setattr(ow, "_governed_delay", _delay)
    monkeypatch.setattr(ow.asyncio, "sleep", _none)
    monkeypatch.setattr(cce, "clone_to_channel", _clone)
    monkeypatch.setattr(fe, "record_flood", _rec)
    res = asyncio.new_event_loop().run_until_complete(ow._exec_content_clone(
        None, None, 9, 100,
        {"source_ref": "@src", "target_refs": ["@a", "@b", "@c"], "msg_ids": [1, 2]}))
    return res, calls, floods


def _ok():
    return {"ok": 2, "fail": 0, "errors": [], "flood_wait": 0, "peer_flood": False}


def _pf():
    return {"ok": 0, "fail": 2, "errors": ["PEER_FLOOD"], "flood_wait": 0, "peer_flood": True}


def test_peer_flood_switches_to_next_account(monkeypatch):
    res, calls, floods = _run(
        monkeypatch, lambda acc, tgt: _pf() if acc == 1 else _ok())
    assert res["status"] == "done" and res["ok"] == 3 and res["failed"] == 0
    assert calls[:2] == [(1, "@a"), (2, "@a")]  # та же цель — следующим аккаунтом
    assert all(a == 2 for a, _ in calls[1:])
    assert floods == [1]


def test_all_accounts_limited_still_stops(monkeypatch):
    res, calls, _ = _run(monkeypatch, lambda acc, tgt: _pf())
    assert res["status"] == "failed"
    assert calls == [(1, "@a"), (2, "@a")]
