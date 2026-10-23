"""Проба живости ботов: getMe по ВСЕМ активным, не только по тем, кому пишут.

Жалоба владельца: «не видно, какие боты живы, а какие мёртвые». Статус бота
(fail_streak/last_ok_at/last_error) раньше обновлялся только при опросе
входящих, а опрос берёт лишь ботов с активностью. Бот без трафика с отозванным
токеном вечно показывался «🟢 Работает». probe_bot_health делает getMe по всем
и пишет здоровье тем же путём, что опрос.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import auto_responder as ar


class _Pool:
    def __init__(self, bots):
        self._bots = bots

    async def fetch(self, q, *a):
        return self._bots


@pytest.mark.asyncio
async def test_probe_marks_live_and_dead(monkeypatch):
    bots = [{"bot_id": 1, "token": "t1"},
            {"bot_id": 2, "token": "t2"},
            {"bot_id": 3, "token": "t3"}]

    async def fake_call(http, token, method):
        assert method == "getMe"
        return {
            "t1": {"ok": True, "result": {"id": 1}},
            "t2": {"ok": False, "error_code": 401, "description": "Unauthorized"},
            "t3": {"ok": False, "error_code": 500, "description": "Internal"},
        }[token]

    oks, errs = [], []

    async def rec_ok(pool, bid):
        oks.append(bid)

    async def rec_err(pool, bid, desc, ec=None):
        errs.append((bid, ec))

    monkeypatch.setattr(ar.bot_api, "_call", fake_call)
    monkeypatch.setattr(ar, "_record_bot_ok", rec_ok)
    monkeypatch.setattr(ar, "_record_bot_error", rec_err)
    monkeypatch.setattr(ar.asyncio, "sleep", AsyncMock())

    res = await ar.probe_bot_health(_Pool(bots), None)

    assert res == {"checked": 3, "ok": 1, "err": 2}
    assert oks == [1], "живой бот не отмечен ok"
    assert (2, 401) in errs, "отозванный токен не записан"
    assert (3, 500) in errs, "сетевой/серверный сбой не записан"


@pytest.mark.asyncio
async def test_probe_records_network_exception_as_error(monkeypatch):
    bots = [{"bot_id": 9, "token": "tx"}]

    async def boom(http, token, method):
        raise RuntimeError("network down")

    errs = []

    async def rec_err(pool, bid, desc, ec=None):
        errs.append(bid)

    monkeypatch.setattr(ar.bot_api, "_call", boom)
    monkeypatch.setattr(ar, "_record_bot_error", rec_err)
    monkeypatch.setattr(ar, "_record_bot_ok", AsyncMock())
    monkeypatch.setattr(ar.asyncio, "sleep", AsyncMock())

    res = await ar.probe_bot_health(_Pool(bots), None)
    assert res["err"] == 1 and errs == [9], "исключение getMe не записано как ошибка бота"


def test_probe_is_wired_into_run_loop():
    import inspect
    src = inspect.getsource(ar.run)
    assert "probe_bot_health" in src, "проба живости не вызывается в главном цикле"
