"""Щит читает риск флота одним запросом, и решения те же, что поштучно.

`run_owner` звал `get_account_risk` на каждый аккаунт: 1667 аккаунтов —
1667 запросов (0,43 с), теперь `get_account_risks` — один (0,15 с).
"""
from __future__ import annotations

import asyncio

from services import physics_engine as pe


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ROWS = {
    1: {"account_id": 1, "risk_score": 0.9, "ban_probability": 0.7, "flood_rate_1h": 2.0,
        "ops_24h": 40, "last_flood_at": None, "factors": '["flood"]', "computed_at": None},
    2: {"account_id": 2, "risk_score": 0.1, "ban_probability": 0.0, "flood_rate_1h": 0.0,
        "ops_24h": 3, "last_flood_at": None, "factors": None, "computed_at": None},
}


class _Pool:
    def __init__(self):
        self.calls = 0

    async def fetchrow(self, q, *a):
        self.calls += 1
        return ROWS.get(int(a[0]))

    async def fetch(self, q, *a):
        self.calls += 1
        return [ROWS[i] for i in a[0] if i in ROWS]


def test_batch_equals_one_by_one():
    p = _Pool()
    batch = _run(pe.get_account_risks(p, [1, 2, 3]))
    assert p.calls == 1
    for a in (1, 2, 3):
        assert batch[a] == _run(pe.get_account_risk(_Pool(), a)), a


def test_batch_failure_is_zero_risk_like_single():
    class Boom:
        async def fetch(self, *a):
            raise RuntimeError("db down")

        async def fetchrow(self, *a):
            raise RuntimeError("db down")

    assert _run(pe.get_account_risks(Boom(), [5])) == {5: _run(pe.get_account_risk(Boom(), 5))}


def test_run_owner_uses_the_batch():
    import inspect

    from services import account_shield as sh
    src = inspect.getsource(sh.run_owner)
    assert "get_account_risks(" in src and "evaluate_account(" not in src
