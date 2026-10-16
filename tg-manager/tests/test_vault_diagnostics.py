"""Диагностика «зависшего» хранилища: почему нет свежих диалогов.

Жалоба: хранилище перестало писать историю ~неделю назад. Раньше статус говорил
только connected/нет. Теперь diagnostics различает: never / disabled / stale / ok
— чтобы UI показал причину и как переподключить.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from services import vault_service as v


class _FakePool:
    def __init__(self, conn_row, msg_row):
        self._conn = conn_row
        self._msg = msg_row

    async def fetchrow(self, sql, *args):
        if "business_connections" in sql:
            return self._conn
        return self._msg


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _dt_days_ago(n):
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n)


def test_never_connected():
    d = _run(v.diagnostics(_FakePool(None, {"last_at": None, "total": 0}), 1))
    assert d["health"] == "never"
    assert d["is_enabled"] is False


def test_disabled_connection():
    conn = {"is_enabled": False, "updated_at": _dt_days_ago(10)}
    d = _run(v.diagnostics(_FakePool(conn, {"last_at": _dt_days_ago(8), "total": 100}), 1))
    assert d["health"] == "disabled"


def test_stale_when_enabled_but_no_fresh_messages():
    conn = {"is_enabled": True, "updated_at": _dt_days_ago(30)}
    # последнее сообщение 7 дней назад — «зависло»
    d = _run(v.diagnostics(_FakePool(conn, {"last_at": _dt_days_ago(7), "total": 500}), 1))
    assert d["health"] == "stale"
    assert d["stale_days"] == 7


def test_ok_when_fresh():
    conn = {"is_enabled": True, "updated_at": _dt_days_ago(1)}
    d = _run(v.diagnostics(_FakePool(conn, {"last_at": _dt_days_ago(0), "total": 500}), 1))
    assert d["health"] == "ok"
    assert d["total_messages"] == 500
