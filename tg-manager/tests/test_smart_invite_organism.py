"""Безопасный инвайтинг (governor чата) → организм: паузы приёма в мозг.

smart_invite морозит приём в чат при chat-flood/негативе/мёртвой активности, но
организм об этом не знал. Теперь world._invite_chats считает замороженные/мёртвые
чаты, а мозг предупреждает «лить туда — гарантированный флуд».
"""
from __future__ import annotations

import asyncio
import os

from services.organism.brain import build_suggestions
from services.organism import world

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _snap(**over):
    base = {
        "fleet": {"accounts": 20, "active": 18, "dead": 0, "restricted": 0,
                  "pressure": 20, "governor_mult": 1.0, "governor_level": "green",
                  "geo": {}, "bans_24h": 0},
        "ops": {"running": 1, "pending": 0, "failed_24h": 0, "last_failed": None},
        "graph": {"contacts": 500, "hot_leads": 0, "intents_24h": 0},
        "vault": {"health": "ok"}, "goal": {"label": "x"},
        "invite_chats": {"frozen": 0, "dead": 0},
        "bots": {"total": 0, "active": 0, "inactive": 0},
    }
    for k, v in over.items():
        base[k] = {**(base.get(k) or {}), **v} if isinstance(v, dict) else v
    return base


def test_frozen_chats_raise_suggestion():
    s = build_suggestions(_snap(invite_chats={"frozen": 2, "dead": 1}))
    r = next((x for x in s if x["id"] == "invite_chats_frozen"), None)
    assert r is not None and r["action"]["kind"] == "invite"
    assert "2" in r["title"] and r["severity"] == "warn"


def test_no_frozen_no_suggestion():
    assert not any(x["id"] == "invite_chats_frozen" for x in build_suggestions(_snap()))
    base = _snap(); del base["invite_chats"]
    assert not any(x["id"] == "invite_chats_frozen" for x in build_suggestions(base))


class _FakePool:
    def __init__(self, frozen, dead):
        self._r = {"frozen": frozen, "dead": dead}

    async def fetchrow(self, q, *a):
        return self._r


def test_world_counts_frozen_and_dead():
    out = _run(world._invite_chats(_FakePool(3, 2), 1))
    assert out["frozen"] == 3 and out["dead"] == 2


def test_world_invite_chats_fail_open():
    class Boom:
        async def fetchrow(self, q, *a):
            raise RuntimeError("db down")
    assert _run(world._invite_chats(Boom(), 1)) == {"frozen": 0, "dead": 0}


def test_world_snapshot_wires_invite_chats():
    src = open(os.path.join(ROOT, "services", "organism", "world.py"), encoding="utf-8").read()
    assert "async def _invite_chats" in src
    assert '"invite_chats": await _invite_chats(' in src
    assert "chat_invite_state" in src
