"""Спина организма: шина событий (emit→persist+dispatch), память, общий контекст."""
from __future__ import annotations

import asyncio
import json

from services.organism import spine


class _FakePool:
    def __init__(self):
        self.execs = []
        self._state = {}

    async def execute(self, sql, *a):
        self.execs.append((sql, a))
        if "organism_state" in sql:
            self._state[(a[0], a[1])] = a[2]
        return "INSERT 0 1"

    async def fetchrow(self, sql, *a):
        if "organism_state" in sql:
            v = self._state.get((a[0], a[1]))
            return {"value": v} if v is not None else None
        return None

    async def fetch(self, sql, *a):
        return []


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def setup_function(_):
    spine._clear()


def test_emit_persists_and_dispatches():
    pool = _FakePool()
    seen = []

    async def handler(owner, kind, payload, p):
        seen.append((owner, kind, payload))

    spine.on("op_done", handler)
    _run(spine.emit(pool, 7, "op_done", {"op_id": 1, "status": "done"}))
    # записалось в память
    assert any("organism_events" in e[0] for e in pool.execs)
    # разбудило подписчика
    assert seen == [(7, "op_done", {"op_id": 1, "status": "done"})]


def test_wildcard_subscriber():
    pool = _FakePool()
    seen = []

    async def all_handler(owner, kind, payload, p):
        seen.append(kind)

    spine.on("*", all_handler)
    _run(spine.emit(pool, 1, "intent", {}))
    _run(spine.emit(pool, 1, "ban", {}))
    assert seen == ["intent", "ban"]


def test_handler_failure_isolated():
    pool = _FakePool()

    async def boom(owner, kind, payload, p):
        raise RuntimeError("x")

    good = []

    async def good_h(owner, kind, payload, p):
        good.append(kind)

    spine.on("x", boom)
    spine.on("x", good_h)
    _run(spine.emit(pool, 1, "x", {}))   # не падает; второй подписчик отработал
    assert good == ["x"]


def test_state_roundtrip():
    pool = _FakePool()
    _run(spine.state_set(pool, 5, "goal", {"target": 1000}))
    got = _run(spine.state_get(pool, 5, "goal"))
    assert got == {"target": 1000}
    assert _run(spine.state_get(pool, 5, "missing", "def")) == "def"


def test_emit_failopen_without_pool():
    # pool=None не должен ронять emit (шина best-effort)
    seen = []

    async def h(o, k, p, pool):
        seen.append(k)

    spine.on("z", h)
    _run(spine.emit(None, 1, "z", {}))
    assert seen == ["z"]
