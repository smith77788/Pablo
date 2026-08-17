"""Жизненный цикл организма: проактивный нудж с антиспам-кулдауном."""
from __future__ import annotations

import asyncio

from services.organism import runner, brain


class _FakePool:
    def __init__(self):
        self._state = {}

    async def fetchrow(self, sql, *a):
        if "organism_state" in sql:
            v = self._state.get((a[0], a[1]))
            return {"value": v} if v is not None else None
        return None

    async def execute(self, sql, *a):
        if "organism_state" in sql:
            self._state[(a[0], a[1])] = a[2]
        return "OK"

    async def fetch(self, sql, *a):
        return []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_pulse(monkeypatch, suggestions):
    async def fake_pulse(pool, owner_id):
        return {"narrative": "", "snapshot": {}, "suggestions": suggestions}
    monkeypatch.setattr(brain, "pulse", fake_pulse)


def test_nudge_sent_for_urgent(monkeypatch):
    _patch_pulse(monkeypatch, [{"id": "vault_off", "severity": "urgent",
                                "title": "Хранилище", "why": "переподключите", "action": {}}])
    pool = _FakePool()
    sent = []
    async def notifier(owner, msg): sent.append((owner, msg))
    ok = _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=1000.0))
    assert ok is True and sent and sent[0][0] == 1
    assert "Хранилище" in sent[0][1]


def test_info_only_no_nudge(monkeypatch):
    _patch_pulse(monkeypatch, [{"id": "idle", "severity": "info",
                                "title": "Свободно", "why": "...", "action": {}}])
    pool = _FakePool()
    sent = []
    async def notifier(o, m): sent.append(1)
    ok = _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=1000.0))
    assert ok is False and not sent   # info не будит


def test_same_suggestion_cooldown(monkeypatch):
    _patch_pulse(monkeypatch, [{"id": "vault_off", "severity": "urgent",
                                "title": "X", "why": "y", "action": {}}])
    pool = _FakePool()
    sent = []
    async def notifier(o, m): sent.append(1)
    t0 = 1000.0
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=t0)) is True
    # через 1 час — та же подсказка, кулдаун 6ч → молчим
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=t0 + 3600)) is False
    # через 7 часов — снова можно
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=t0 + 7 * 3600)) is True
    assert len(sent) == 2


def test_global_gap_between_different(monkeypatch):
    pool = _FakePool()
    sent = []
    async def notifier(o, m): sent.append(1)
    t0 = 1000.0
    _patch_pulse(monkeypatch, [{"id": "a", "severity": "urgent", "title": "A", "why": "y", "action": {}}])
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=t0)) is True
    # другая подсказка через 30 мин — глобальный зазор 2ч не пускает
    _patch_pulse(monkeypatch, [{"id": "b", "severity": "warn", "title": "B", "why": "y", "action": {}}])
    assert _run(runner._tick_owner(pool, None, 1, notifier=notifier, now=t0 + 1800)) is False
