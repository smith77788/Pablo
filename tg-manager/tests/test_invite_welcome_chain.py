"""Шов «Инвайт → Welcome»: после успешного инвайта приветствие вступившим.

Проверяем _chain_welcome: берёт ok-цели из operation_log, ставит bulk_dm_adhoc
чанками ≤1000; не срабатывает для метода 'link' и без welcome; wiring на входе
(mass_inviter_submit кладёт params['welcome']).
"""
from __future__ import annotations

import asyncio
import os

from services import op_worker


class _FakePool:
    def __init__(self, ok_targets):
        self._targets = ok_targets
        self.submitted = []

    async def fetch(self, sql, *a):
        if "operation_log" in sql:
            return [{"target": t} for t in self._targets]
        if "tg_accounts" in sql:
            return [{"id": 1}, {"id": 2}]
        return []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_chain_welcome_enqueues_for_joiners(monkeypatch):
    subs = []
    async def fake_submit(pool, owner, op_type, params, **kw):
        subs.append((op_type, params))
        return 999
    import services.operation_bus as ob
    monkeypatch.setattr(ob, "submit", fake_submit)

    pool = _FakePool(["@a", "@b", "123", "promote"])   # promote — мета, отфильтровать
    _run(op_worker._chain_welcome(pool, 1, 55,
        {"invite_method": "direct", "account_ids": [1, 2],
         "welcome": {"text": "Привет!", "delay": 30}}))
    assert len(subs) == 1
    op_type, params = subs[0]
    assert op_type == "bulk_dm_adhoc"
    assert params["usernames"] == ["@a", "@b", "123"]   # без 'promote'
    assert params["text"] == "Привет!" and params["account_ids"] == [1, 2]


def test_no_welcome_no_action(monkeypatch):
    subs = []
    async def fake_submit(*a, **k): subs.append(1); return 1
    import services.operation_bus as ob
    monkeypatch.setattr(ob, "submit", fake_submit)
    pool = _FakePool(["@a"])
    _run(op_worker._chain_welcome(pool, 1, 55, {"invite_method": "direct"}))
    assert not subs


def test_link_method_skips_welcome(monkeypatch):
    subs = []
    async def fake_submit(*a, **k): subs.append(1); return 1
    import services.operation_bus as ob
    monkeypatch.setattr(ob, "submit", fake_submit)
    pool = _FakePool(["@a"])
    _run(op_worker._chain_welcome(pool, 1, 55,
        {"invite_method": "link", "welcome": {"text": "hi"}}))
    assert not subs   # link = приглашение уже есть DM


def test_submit_wires_welcome_param():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "mini_app_api.py"), encoding="utf-8").read()
    assert 'body.get("welcome_message")' in src
    assert 'params["welcome"]' in src
