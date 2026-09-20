"""Владелец может ПРИНУДИТЕЛЬНО взять рисковые аккаунты в инвайт.

Жалоба владельца: «рисковые аккаунты нельзя взять в работу, если очень нужно;
из 52 аккаунтов работают ~20». Рисковые (недавнее ограничение → карантин
риск-пульса) и на кулдауне по умолчанию отсеиваются ДВУМЯ гейтами:
  * select_all_active(respect_cooldown=True) — режет cooldown_until в будущем;
  * is_account_quarantined — режет недавно ограниченные.
Оба автоматические, без ручки. include_risky=True в параметрах операции снимает
ОБА мягких гейта (жёсткие banned/spamblock остаются) — осознанный выбор владельца.

Стенд повторяет харнесс test_invite_queue_scheduler, но даёт управлять карантином
и ловит, с каким respect_cooldown позвали select_all_active.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker


@pytest.fixture(autouse=True)
def _reset_account_claims():
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()
    yield
    op_worker._accounts_in_use.clear()
    op_worker._operation_account_locks.clear()


class _Pool:
    def __init__(self):
        self.done_items = 0

    async def execute(self, q, *a):
        if "done_items=done_items+" in q:
            self.done_items += int(a[1])
        return "OK"

    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []


ACCOUNTS = [
    {"id": 1, "session_str": "s1", "proxy_url": None},
    {"id": 2, "session_str": "s2", "proxy_url": None},  # «рисковый» в тестах ниже
]


async def _anoop(*a, **k):
    return None


def _false():
    async def _f():
        return False
    return _f()


@pytest.fixture
def stand(monkeypatch):
    import services.mass_inviter_engine as inv
    from services import flood_engine as fe

    state = {"quarantined": set(), "respect_cooldown_seen": []}

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(op_worker, "_is_cancelled", lambda *a, **k: _false())
    monkeypatch.setattr(op_worker, "_safe_execute", _anoop)
    monkeypatch.setattr(fe, "recommended_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(fe, "gaussian_delay", lambda base, **k: 0.0)
    monkeypatch.setattr(fe, "record_success", _anoop)
    monkeypatch.setattr(fe, "record_peer_flood", _anoop)
    monkeypatch.setattr(fe, "record_flood", _anoop)

    calls: list[int] = []

    async def _invite_batch(session_str, acc, group, refs, pace_mult=1.0, bulk=None):
        calls.append(int(acc["id"]))
        return {"ok": len(refs), "failed": 0, "errors": []}

    monkeypatch.setattr(inv, "invite_batch", _invite_batch)
    monkeypatch.setattr(inv, "invite_by_phones", _invite_batch)

    async def _limit(pool, acc_id):
        return {"limit": 1000, "used_today": 0, "remaining": 1000, "basis": "тест"}

    monkeypatch.setattr(fe, "recommended_daily_limit", _limit)

    # select_all_active честно уважает respect_cooldown: карантинных оно НЕ режет
    # (это делает is_account_quarantined отдельно), а вот кулдаун — по флагу.
    async def _sel_all(pool, owner_id, **k):
        state["respect_cooldown_seen"].append(k.get("respect_cooldown"))
        return list(ACCOUNTS)
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel_all)

    async def _accounts(pool, q, *a):
        return ACCOUNTS if "LEFT JOIN user_proxies" in q else []
    monkeypatch.setattr(op_worker, "_safe_fetch", _accounts)

    async def _quar(pool, acc_id, **k):
        return int(acc_id) in state["quarantined"]
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _quar)

    return state, calls


def _run(pool, refs, **params):
    p = {"group": "@g", "account_ids": [1, 2], "user_refs": refs, "batch_size": 5}
    p.update(params)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(op_worker._exec_mass_invite(pool, None, 91, 100, p))
    finally:
        loop.run_until_complete(op_worker.release_operation_accounts(91))
        loop.close()


TARGETS = [f"u{i}" for i in range(1, 21)]


def test_risky_account_is_excluded_by_default(stand):
    """Аккаунт в карантине по умолчанию НЕ берётся — работает только здоровый."""
    state, calls = stand
    state["quarantined"].add(2)
    _run(_Pool(), TARGETS)
    assert set(calls) == {1}, f"карантинный аккаунт не должен работать без форса: {calls}"


def test_include_risky_forces_quarantined_account(stand):
    """include_risky=True берёт карантинный аккаунт в работу («если очень нужно»)."""
    state, calls = stand
    state["quarantined"].add(2)
    res = _run(_Pool(), TARGETS, include_risky=True)
    assert set(calls) == {1, 2}, f"форс не задействовал рисковый аккаунт: {calls}"
    assert "рисковые ПРИНУДИТЕЛЬНО" in res["summary"], (
        f"итог обязан честно назвать форсирование рисковых: {res['summary']!r}")


def test_include_risky_drops_cooldown_gate(stand):
    """include_risky=True зовёт select_all_active с respect_cooldown=False —
    аккаунты на кулдауне тоже попадают в выборку."""
    state, calls = stand
    _run(_Pool(), TARGETS, include_risky=True)
    assert state["respect_cooldown_seen"] == [False], (
        f"кулдаун-гейт не снят при include_risky: {state['respect_cooldown_seen']}")


def test_default_keeps_cooldown_gate(stand):
    """Без include_risky кулдаун-гейт на месте (respect_cooldown=True)."""
    state, calls = stand
    _run(_Pool(), TARGETS)
    assert state["respect_cooldown_seen"] == [True], (
        f"кулдаун-гейт должен быть включён по умолчанию: {state['respect_cooldown_seen']}")
