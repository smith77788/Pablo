"""Разовая рассылка ЛС: флуд аккаунта не теряет получателя (регресс).

Было: получатель, на котором аккаунт поймал FloodWait/PeerFlood, писался в
ошибки и больше не получал сообщения. При единственном аккаунте следующий
получатель уходил с того же аккаунта прямо во флуд. Остановка по лимиту на
аккаунт писалась в журнал как 'skip' — повтор считал получателя закрытым.
"""
from __future__ import annotations

import asyncio

from services import account_manager as am
from services import op_worker as ow


def _run(monkeypatch, n_acc, send, params_extra=None):
    accs = [{"id": i, "session_str": "s"} for i in range(1, n_acc + 1)]
    logs: list = []
    calls: list = []

    async def _sel(*a, **k):
        return accs

    async def _claim(ids):
        return list(ids)

    async def _none(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _settled(*a, **k):
        return {}

    async def _delay(*a, **k):
        return 0

    async def _exec(pool, q, *args, **k):
        if "INSERT INTO operation_log" in q:
            logs.append((args[2], args[3]))

    async def _send(sess, username, text, _acc=None):
        calls.append((_acc["id"], username))
        return send(_acc["id"], username)

    async def _quar(pool, acc_id):
        return False

    async def _opt(*a, **k):
        return set()

    from services import contact_opt_out as coo

    monkeypatch.setattr(ow.resource_selector, "select_all_active", _sel)
    monkeypatch.setattr(ow, "try_claim_accounts", _claim)
    monkeypatch.setattr(ow, "release_accounts", _none)
    monkeypatch.setattr(ow, "_safe_execute", _exec)
    monkeypatch.setattr(ow, "_is_cancelled", _false)
    monkeypatch.setattr(ow, "settled_targets", _settled)
    monkeypatch.setattr(ow, "_governed_delay", _delay)
    monkeypatch.setattr(ow._infra_mem, "is_account_quarantined", _quar)
    monkeypatch.setattr(coo, "load_opted_out", _opt)
    monkeypatch.setattr(ow.asyncio, "sleep", _none)
    monkeypatch.setattr(ow, "bounded_flood_sleep", _none)
    monkeypatch.setattr(am, "send_dm", _send)
    params = {"account_ids": [a["id"] for a in accs], "usernames": ["u1", "u2"],
              "text": "привет"}
    params.update(params_extra or {})
    res = asyncio.new_event_loop().run_until_complete(
        ow._exec_bulk_dm_adhoc(None, None, 5, 100, params))
    return res, calls, logs


def test_flood_reroutes_recipient_to_next_account(monkeypatch):
    def send(acc, u):
        if acc == 1:
            return {"error": "FloodWait 30", "flood_wait": 30}
        return {"ok": True}

    res, calls, _ = _run(monkeypatch, 2, send)
    assert res["ok"] == 2 and res["fail"] == 0
    assert (2, "u1") in calls


def test_peer_flood_reroutes_recipient(monkeypatch):
    def send(acc, u):
        if acc == 1:
            return {"error": "PEER_FLOOD", "peer_flood": True}
        return {"ok": True}

    res, _, _ = _run(monkeypatch, 2, send)
    assert res["ok"] == 2 and res["fail"] == 0


def test_long_flood_on_last_account_defers_operation(monkeypatch):
    res, calls, _ = _run(
        monkeypatch, 1, lambda a, u: {"error": "FloodWait", "flood_wait": 7200})
    assert res["status"] == "requeue" and res["defer_s"] == 7200
    assert calls == [(1, "u1")], "следующий получатель не должен уходить во флуд"


def test_cap_stop_is_not_logged_as_settled_skip(monkeypatch):
    res, _, logs = _run(monkeypatch, 1, lambda a, u: {"ok": True},
                        {"per_account_cap": 1})
    assert res["ok"] == 1
    assert ("u2", "skip") not in logs
