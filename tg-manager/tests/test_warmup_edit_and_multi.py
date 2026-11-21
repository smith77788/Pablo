"""Прогрев: план на несколько аккаунтов сразу и правка идущего плана.

* Форма заводила план строго на один аккаунт — десять аккаунтов означали
  десять заходов в одну и ту же форму. Теперь `account_ids` идёт через ту же
  `create_warmup_plan` (одни проверки), а неподходящий аккаунт не валит пакет.
* Правки плана не было: менять темп можно было только перезапуском, а
  перезапуск (`create_warmup_plan`) снимает паузу. `update_warmup_plan` меняет
  настройки и оставляет статус и день как были.
"""
from __future__ import annotations

import asyncio
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from services import account_warmer as aw


class _Ctx:
    def __init__(self, v):
        self.v = v

    async def __aenter__(self):
        return self.v

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, row):
        self.row = row
        self.calls: list[tuple] = []

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return self.row

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "OK"

    def transaction(self):
        return _Ctx(None)


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    def acquire(self):
        return _Ctx(self.conn)


def test_update_keeps_pause_and_clamps():
    row = {"account_id": 7, "daily_actions": 20, "target_days": 5,
           "status": "paused", "current_day": 3}
    pool = _Pool(row)
    out = asyncio.run(aw.update_warmup_plan(pool, 42, 11, daily_actions=99, target_days=1))
    assert out["status"] == "paused"
    q, args = pool.conn.calls[0]
    assert "UPDATE account_warmup_plans" in q
    assert "status='active'" not in q.replace(" ", ""), "правка не должна снимать паузу"
    assert "owner_id=$2" in q and "status IN ('active','paused')" in q
    assert args[2] == 20 and args[3] == 5          # пределы 3–20 и 5–45
    # Профиль не трогаем, если о нём не просили.
    assert not any("account_niche_profiles" in c[0] for c in pool.conn.calls)


def test_update_profile_keeps_unset_fields():
    pool = _Pool({"account_id": 7, "daily_actions": 10, "target_days": 14,
                  "status": "active", "current_day": 1})
    asyncio.run(aw.update_warmup_plan(pool, 42, 11, niche="crypto"))
    ins = [c for c in pool.conn.calls if "INSERT INTO account_niche_profiles" in c[0]]
    assert ins, "ниша не записана"
    q, args = ins[0]
    assert "COALESCE($4, account_niche_profiles.profile_type)" in q
    assert args[2] == "crypto" and args[3] is None


def test_update_unknown_plan_is_404():
    pool = _Pool(None)
    try:
        asyncio.run(aw.update_warmup_plan(pool, 42, 11, daily_actions=5))
    except aw.WarmupNotAllowed as exc:
        assert exc.status == 404
    else:
        raise AssertionError("нет отказа на чужой/завершённый план")


async def _call(monkeypatch, method, path, body):
    from services import mini_app_api, mini_app_warmup

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda r: 42)
    app = web.Application()
    mini_app_warmup.setup_routes(app, object())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.request(method, path, data=json.dumps(body))
        return r.status, await r.json()
    finally:
        await client.close()


def test_create_for_several_accounts(monkeypatch):
    from services import mini_app_api

    created = []

    async def _create(pool, uid, aid, plan_type, **kw):
        if aid == 3:
            raise aw.WarmupNotAllowed("Аккаунт забанен", 409)
        created.append((aid, plan_type, kw["profile_type"], kw["daily_actions"]))
        return 100 + aid

    submitted = []

    async def _submit(pool, uid, op, params, **kw):
        submitted.append(params["account_id"])
        return len(submitted)

    monkeypatch.setattr(aw, "create_warmup_plan", _create)
    monkeypatch.setattr(mini_app_api._obus, "submit", _submit)
    status, data = asyncio.run(_call(monkeypatch, "POST", "/api/miniapp/warmup", {
        "account_ids": [1, 2, 3, 2], "plan_type": "gentle",
        "profile_type": "reader", "daily_actions": 6}))
    assert status == 200
    assert data["started"] == 2 and data["account_ids"] == [1, 2]
    assert data["refused"] == [{"account_id": 3, "reason": "Аккаунт забанен"}]
    assert [c[0] for c in created] == [1, 2] and created[0][1:] == ("gentle", "reader", 6)
    assert submitted == [1, 2]


def test_edit_route(monkeypatch):
    seen = {}

    async def _upd(pool, uid, plan_id, **kw):
        seen.update(kw, uid=uid, plan_id=plan_id)
        return {"daily_actions": 8, "target_days": 20, "status": "paused", "current_day": 4}

    monkeypatch.setattr(aw, "update_warmup_plan", _upd)
    status, data = asyncio.run(_call(monkeypatch, "PUT", "/api/miniapp/warmup/11",
                                     {"daily_actions": "8", "niche": "", "custom_channels": "@chan_a\n@chan_b"}))
    assert status == 200 and data["status"] == "paused"
    assert seen["uid"] == 42 and seen["plan_id"] == 11 and seen["daily_actions"] == 8
    assert seen["niche"] is None and seen["profile_type"] is None
    assert seen["custom_channels"] == ["@chan_a", "@chan_b"]


def test_chat_warmup_takes_several_chats_at_once():
    """Разогрев чата: несколько чатов одной строкой — по сессии на чат."""
    import os
    import re
    import shutil
    import subprocess

    import pytest

    html = open(os.path.join(os.path.dirname(__file__), "..", "mini_app", "index.html"),
                encoding="utf-8").read()
    m = re.search(r"function _cwChats\(raw\)\{.*?\n\}", html, re.S)
    assert m, "нет разбора нескольких чатов"
    body = html[html.index("async function cwStart(){"):html.index("let CW_DETAIL_ID = 0;")]
    assert "for (const chat of chats)" in body and "/api/miniapp/chatwarmup/session" in body
    if not shutil.which("node"):
        pytest.skip("нет node")
    js = m.group(0) + "\nconsole.log(JSON.stringify(_cwChats('@a, t.me/b\\n-100123 @a')))"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["@a", "t.me/b", "-100123"]
