"""Паритет: повтор рассылки недоставленным — теперь и в боте.

broadcast_resend был только в mini-app. Логика вынесена в общий
broadcaster.resend_undelivered (одна реализация на оба фронтенда), бот получил
кнопку «↻ Отправить недоставленным» в карточке рассылки.
"""
from __future__ import annotations

import os

import pytest

from services import broadcaster

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _Pool:
    """Диспатчит по подстроке запроса; фиксирует INSERT'ы."""

    def __init__(self, *, src, bot, undelivered):
        self._src = src
        self._bot = bot
        self._undelivered = undelivered
        self.inserted_broadcast = None
        self.inserted_op = None

    async def fetchrow(self, q, *a):
        if "FROM broadcasts WHERE id=" in q:
            return self._src
        if "FROM managed_bots" in q:
            return self._bot
        if q.startswith("INSERT INTO broadcasts"):
            self.inserted_broadcast = a
            return {"id": 777}
        return None

    async def fetch(self, q, *a):
        if "FROM bot_users" in q:
            return self._undelivered
        return []

    async def fetchval(self, q, *a):
        if q.startswith("INSERT INTO operation_queue"):
            self.inserted_op = a
            return 888
        return None


@pytest.mark.asyncio
async def test_resend_creates_op_for_undelivered():
    pool = _Pool(
        src={"id": 1, "bot_id": 10, "message_text": "hi", "created_by": 42},
        bot={"bot_id": 10},
        undelivered=[{"user_id": 100}, {"user_id": 101}, {"user_id": 102}],
    )
    res = await broadcaster.resend_undelivered(pool, owner_id=42, bc_id=1)
    assert res["ok"] is True
    assert res["total_users"] == 3
    assert res["broadcast_id"] == 777
    assert res["op_id"] == 888
    # op_type run_broadcast + user_ids реально долетают
    import json
    op_params = json.loads(pool.inserted_op[1])
    assert op_params["user_ids"] == [100, 101, 102]
    assert op_params["bot_id"] == 10


@pytest.mark.asyncio
async def test_resend_rejects_foreign_owner():
    pool = _Pool(
        src={"id": 1, "bot_id": 10, "message_text": "hi", "created_by": 999},
        bot={"bot_id": 10}, undelivered=[],
    )
    res = await broadcaster.resend_undelivered(pool, owner_id=42, bc_id=1)
    assert res["ok"] is False and res["code"] == 404


@pytest.mark.asyncio
async def test_resend_when_all_delivered():
    pool = _Pool(
        src={"id": 1, "bot_id": 10, "message_text": "hi", "created_by": 42},
        bot={"bot_id": 10}, undelivered=[],
    )
    res = await broadcaster.resend_undelivered(pool, owner_id=42, bc_id=1)
    assert res["ok"] is False and res["code"] == 400
    assert pool.inserted_broadcast is None  # ничего не создаём


def test_bot_and_app_wired_to_shared_impl():
    b = _read("bot/handlers/broadcast.py")
    assert 'BroadcastCb.filter(F.action == "resend")' in b
    assert "broadcaster.resend_undelivered(" in b
    api = _read("services/mini_app_api.py")
    assert "broadcaster.resend_undelivered(pool, uid, bc_id)" in api
    kb = _read("bot/keyboards.py")
    assert 'action="resend"' in kb
