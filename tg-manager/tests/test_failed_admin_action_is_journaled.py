"""Упавшее админ-действие мини-аппа тоже попадает в журнал.

Слой журнала писал запись ПОСЛЕ ответа ручки. Если ручка бана или выдачи
тарифа падала исключением (часть дела уже могла быть сделана), исключение
пролетало мимо записи — и попытка не оставляла в admin_audit_log никакого
следа. Падает без фикса: строк журнала ноль.
"""
from __future__ import annotations

import asyncio

from aiohttp import web

ADMIN = 770055
TARGET = 4242


class _Pool:
    def __init__(self):
        self.audit = []

    async def fetchval(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def execute(self, q, *a):
        if "admin_audit_log" in q:
            self.audit.append(a)
        return "OK"


def test_admin_action_that_raises_is_still_journaled(monkeypatch):
    from aiohttp.test_utils import make_mocked_request
    from services import mini_app_api as M

    monkeypatch.setattr(M, "_is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(M, "_get_uid", lambda r: ADMIN)
    pool = _Pool()
    app = web.Application()
    M.setup_routes(app, pool)
    mw = next(m for m in app.middlewares
              if getattr(m, "__name__", "") == "admin_audit_middleware")

    req = make_mocked_request(
        "POST", f"/api/miniapp/admin/user/{TARGET}/ban",
        match_info={"user_id": str(TARGET)})

    async def handler(request):
        raise RuntimeError("сбой посреди бана")

    async def go():
        try:
            await mw(req, handler)
        except RuntimeError:
            return True
        return False

    assert asyncio.run(go()), "исключение ручки не должно глотаться журналом"
    assert pool.audit, "упавшая попытка не попала в журнал"
    row = pool.audit[0]
    assert row[0] == ADMIN and row[2] == TARGET
    assert "error" in str(row)
