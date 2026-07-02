"""Тесты guard-путей executor'ов op_worker (обработка ошибок без Telethon).

Используют лёгкий FakePool: проверяем, что executor'ы корректно и БЕЗОПАСНО
возвращают статус при отсутствии данных, а не падают с исключением.
"""
from __future__ import annotations

import pytest


class _AcquireCtx:
    def __init__(self, pool):
        self._pool = pool

    async def __aenter__(self):
        return self._pool

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Минимальный async-«pool»: отдаёт заранее заданные ответы, безопасен."""

    def __init__(self, fetch=None, fetchrow=None, fetchval=None):
        self._fetch = fetch if fetch is not None else []
        self._fetchrow = fetchrow
        self._fetchval = fetchval
        self.calls = []

    async def fetch(self, q, *a):
        self.calls.append(("fetch", q))
        return self._fetch

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q))
        return self._fetchrow

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q))
        return self._fetchval

    async def execute(self, q, *a):
        self.calls.append(("execute", q))
        return "UPDATE 0"

    def acquire(self):
        return _AcquireCtx(self)


class _Bot:
    async def send_message(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_parse_audience_requires_source():
    from services.op_worker import _exec_parse_audience
    res = await _exec_parse_audience(FakePool(), _Bot(), 1, 42, {})
    assert res["status"] == "failed"
    assert "source_ref" in res["summary"]


@pytest.mark.asyncio
async def test_check_accounts_health_no_accounts():
    from services.op_worker import _exec_check_accounts_health
    res = await _exec_check_accounts_health(FakePool(fetch=[]), _Bot(), 1, 42, {})
    assert res["status"] == "failed"
    assert "аккаунт" in res["reason"].lower()


@pytest.mark.asyncio
async def test_bulk_create_channels_multi_no_accounts():
    """Ранее падал KeyError на params['account_ids']; должен возвращать статус."""
    from services.op_worker import _exec_bulk_create_channels_multi
    res = await _exec_bulk_create_channels_multi(FakePool(fetch=[]), _Bot(), 1, 42, {})
    assert isinstance(res, dict)
    assert res.get("status") in ("failed", "done", "error", "cancelled")


@pytest.mark.asyncio
async def test_watchdog_alerts_dedup():
    """Алерт не спамит: повторный прогон с теми же op_id не шлёт заново."""
    from services import op_worker as w
    w._alerted_stuck_ops.clear()
    rows = [{"id": 7, "op_type": "mass_publish", "status": "pending",
             "owner_id": 42, "age_min": 20.0}]
    sent = []

    class _CountBot:
        async def send_message(self, aid, text, **k):
            sent.append(aid)

    # монки: pool.fetch → rows, админы → {1}
    import bot.utils.subscription as sub
    orig_admins = sub._admin_ids
    sub._admin_ids = lambda: {1}
    try:
        await w._watchdog_alerts(FakePool(fetch=rows), _CountBot())
        first = list(sent)
        await w._watchdog_alerts(FakePool(fetch=rows), _CountBot())  # тот же op — без алерта
        assert first == [1]
        assert sent == [1]  # второй раз не добавилось
    finally:
        sub._admin_ids = orig_admins
        w._alerted_stuck_ops.clear()
