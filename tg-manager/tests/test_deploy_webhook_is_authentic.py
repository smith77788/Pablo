"""Деплой-хук принимает только вызов с секретом (D31).

ЧТО БЫЛО. `POST /webhook/deploy` принимал тело от кого угодно (только лимит
20/мин) и рассылал админам «деплой» с веткой, коммитом и статусом из тела —
подделку «💥 CRASHED» или «✅ SUCCESS» мог прислать любой, кто знает адрес.

ЧТО СТАЛО. Нужен `DEPLOY_WEBHOOK_SECRET` — в адресе (`?secret=`, Railway
заголовков не ставит) или в `X-Deploy-Secret`. Без переменной хук закрыт.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from services import payment_webhook as W

_BODY = {"type": "deployment",
         "deployment": {"id": "abcdef12", "status": "CRASHED",
                        "commit": {"branch": "main", "message": "x"}}}


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append(a)


class _Pool:
    async def fetch(self, *a): return []
    async def fetchrow(self, *a): return None
    async def fetchval(self, *a): return 0
    async def execute(self, *a): return "OK"


async def _post(path, headers=None):
    app = W.make_app(_Pool(), _Bot())
    async with TestClient(TestServer(app)) as c:
        r = await c.post(path, data=json.dumps(_BODY), headers=headers or {})
        return r.status


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    async def _ok(*a, **k):
        return True
    monkeypatch.setattr(W, "check_rate_limit", _ok)


def test_closed_without_secret(monkeypatch):
    monkeypatch.delenv("DEPLOY_WEBHOOK_SECRET", raising=False)
    assert asyncio.run(_post("/webhook/deploy")) == 404


def test_wrong_or_missing_secret_is_refused(monkeypatch):
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "s3cret")
    assert asyncio.run(_post("/webhook/deploy")) == 403
    assert asyncio.run(_post("/webhook/deploy?secret=nope")) == 403


def test_right_secret_passes(monkeypatch):
    monkeypatch.setenv("DEPLOY_WEBHOOK_SECRET", "s3cret")
    assert asyncio.run(_post("/webhook/deploy?secret=s3cret")) == 200
    assert asyncio.run(_post("/webhook/deploy",
                             {"X-Deploy-Secret": "s3cret"})) == 200
