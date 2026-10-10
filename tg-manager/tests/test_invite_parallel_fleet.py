"""Флот инвайтит ПАРАЛЛЕЛЬНО, а не по одному по очереди.

ЧТО БЫЛО СЛОМАНО (жалоба владельца: «не все аккаунты начинают инвайтинг, и идёт
не параллельно, а по одному по очереди»). Круг обрабатывал аккаунты строго
последовательно: `for acc in active` с `await inv.invite_batch(...)` на каждый —
второй аккаунт стартовал только после того, как первый ДОСЛАЛ свой батч. Флот из
N аккаунтов слал батчи по одному; при короткой аудитории до дальних аккаунтов
очередь не доходила в том же круге вовсе.

Теперь круг набирает батчи чанками по INVITE_PARALLEL и рассылает чанк
одновременно (сеть параллельно, учёт последовательно). INVITE_PARALLEL=1
возвращает прежнее строго-последовательное поведение.

Стенд повторяет харнесс test_invite_queue_scheduler: зовём _exec_mass_invite
напрямую с фейковым движком, который замеряет, сколько батчей идёт ОДНОВРЕМЕННО.
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


# Четыре аккаунта — чтобы отличить «параллельно по 3» от «по одному».
ACCOUNTS = [{"id": i, "session_str": f"s{i}", "proxy_url": None} for i in (1, 2, 3, 4)]


class _ConcurrencyStand:
    """Фейковый движок: считает, сколько батчей выполняется ОДНОВРЕМЕННО.

    Каждый вызов увеличивает счётчик in-flight, ждёт (пока не наберётся ожидаемое
    число параллельных ЛИБО не выйдет короткий таймаут), потом уменьшает. Пик
    in-flight = реальная степень параллельности, которую дал исполнитель."""

    def __init__(self, expected):
        self.calls: list[int] = []
        self.inflight = 0
        self.max_inflight = 0
        self.expected = expected
        self._gate = asyncio.Event()

    async def invite_batch(self, session_str, acc, group, refs, pace_mult=1.0, bulk=None):
        self.calls.append(int(acc["id"]))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        if self.inflight >= self.expected:
            self._gate.set()   # набралась ожидаемая параллельность — отпускаем всех
        try:
            await asyncio.wait_for(self._gate.wait(), timeout=0.3)
        except (asyncio.TimeoutError, Exception):
            pass
        self.inflight -= 1
        return {"ok": len(refs), "failed": 0, "errors": []}

    async def invite_by_phones(self, session_str, acc, group, refs):
        return await self.invite_batch(session_str, acc, group, refs)


async def _anoop(*a, **k):
    return None


async def _afalse(*a, **k):
    return False


def _false():
    async def _f():
        return False
    return _f()


@pytest.fixture
def stand(monkeypatch):
    import services.mass_inviter_engine as inv
    from services import flood_engine as fe

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(op_worker.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(op_worker, "_is_cancelled", lambda *a, **k: _false())
    monkeypatch.setattr(op_worker, "_safe_execute", _anoop)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _afalse)
    monkeypatch.setattr(fe, "recommended_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(fe, "gaussian_delay", lambda base, **k: 0.0)
    monkeypatch.setattr(fe, "record_success", _anoop)
    monkeypatch.setattr(fe, "record_peer_flood", _anoop)
    monkeypatch.setattr(fe, "record_flood", _anoop)

    def _install(expected):
        s = _ConcurrencyStand(expected)
        monkeypatch.setattr(inv, "invite_batch", s.invite_batch)
        monkeypatch.setattr(inv, "invite_by_phones", s.invite_by_phones)

        async def _limit(pool, acc_id):
            return {"limit": 1000, "used_today": 0, "remaining": 1000, "basis": "тест"}

        monkeypatch.setattr(fe, "recommended_daily_limit", _limit)

        async def _sel_all(pool, owner_id, **k):
            return ACCOUNTS
        monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _sel_all)

        async def _accounts(pool, q, *a):
            return ACCOUNTS if "LEFT JOIN user_proxies" in q else []
        monkeypatch.setattr(op_worker, "_safe_fetch", _accounts)
        return s

    return _install


def _run(pool, refs, **params):
    p = {"group": "@g", "account_ids": [1, 2, 3, 4], "user_refs": refs, "batch_size": 5}
    p.update(params)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(op_worker._exec_mass_invite(pool, None, 77, 100, p))
    finally:
        loop.run_until_complete(op_worker.release_operation_accounts(77))
        loop.close()


# по 5 целей на аккаунт → каждый из 4 аккаунтов получает ровно один батч
TARGETS = [f"u{i}" for i in range(1, 21)]


def test_fleet_invites_in_parallel(stand, monkeypatch):
    """INVITE_PARALLEL=3 → минимум 3 аккаунта инвайтят ОДНОВРЕМЕННО."""
    monkeypatch.setenv("INVITE_PARALLEL", "3")
    s = stand(expected=3)
    res = _run(_Pool(), TARGETS)
    assert s.max_inflight >= 3, (
        f"флот обязан слать параллельно, пик одновременных = {s.max_inflight}")
    assert res["ok"] == len(TARGETS)


def test_sequential_when_parallel_is_one(stand, monkeypatch):
    """INVITE_PARALLEL=1 → строго по одному (откат к прежнему поведению)."""
    monkeypatch.setenv("INVITE_PARALLEL", "1")
    s = stand(expected=1)
    res = _run(_Pool(), TARGETS)
    assert s.max_inflight == 1, (
        f"при INVITE_PARALLEL=1 одновременных быть не должно, пик = {s.max_inflight}")
    assert res["ok"] == len(TARGETS)


def test_all_fleet_accounts_are_engaged(stand, monkeypatch):
    """Все 4 аккаунта реально участвуют, когда целей хватает на всех."""
    monkeypatch.setenv("INVITE_PARALLEL", "4")
    s = stand(expected=4)
    _run(_Pool(), TARGETS)
    assert {a for a in s.calls} == {1, 2, 3, 4}, (
        f"не все аккаунты начали инвайтинг: задействованы {sorted(set(s.calls))}")
