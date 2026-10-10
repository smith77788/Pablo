"""Регрессия: check_accounts_health падал «failed 0/N» при лаге миграции колонок.

_HC_COLS (device-fingerprint + proxy) через _safe_fetch глушил
UndefinedColumnError → [] → «Нет аккаунтов» → операция failed за 1с, 0/6, хотя
аккаунты есть (скрин пользователя: op #85 check_accounts_health failed 0/6).
Теперь: полный набор → фолбэк на минимальный → реальная ошибка (не ложное
«нет аккаунтов»).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker


class _FakePool:
    """fetch: полный запрос (_HC_COLS с device-полями) падает, минимальный — ок."""

    def __init__(self, accounts, fail_full=True, fail_min=False):
        self._accounts = accounts
        self._fail_full = fail_full
        self._fail_min = fail_min
        self.executed = []

    async def fetch(self, query, *a):
        is_full = "device_model" in query
        if is_full and self._fail_full:
            raise Exception('column a.app_version does not exist')
        if (not is_full) and self._fail_min:
            raise Exception('db down')
        return self._accounts

    async def execute(self, query, *a):
        self.executed.append(query)
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


def _run(coro):
    return asyncio.run(coro)


def _acc(i=1):
    return {"id": i, "session_str": "s", "first_name": "A", "phone": "+1", "username": "u", "proxy_url": None}


def test_falls_back_to_minimal_and_processes():
    pool = _FakePool([_acc(1), _acc(2)], fail_full=True, fail_min=False)
    with patch.object(op_worker, "check_account_status_full",
                      AsyncMock(return_value={"status": "active"}), create=True) as _cs, \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("services.account_manager.check_account_status_full", AsyncMock(return_value={"status": "active"})), \
         patch("services.account_manager.should_persist_account_status", lambda *a, **k: False), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_check_accounts_health(pool, None, 5, 99, {"check_spambot": False}))
    # НЕ «нет аккаунтов»: фолбэк нашёл 2 аккаунта и обработал их
    assert res["status"] != "failed" or "Нет аккаунтов" not in res.get("reason", "")
    assert "Проверено" in res.get("summary", "") or res.get("status") == "done"


def test_real_db_error_surfaced_not_masked():
    pool = _FakePool([], fail_full=True, fail_min=True)
    with patch("services.account_manager.check_account_status_full", AsyncMock()), \
         patch("services.account_manager.should_persist_account_status", lambda *a, **k: False):
        res = _run(op_worker._exec_check_accounts_health(pool, None, 5, 99, {}))
    assert res["status"] == "failed" and "Ошибка запроса" in res["reason"]


def test_genuinely_no_accounts_still_reports_no_accounts():
    pool = _FakePool([], fail_full=False, fail_min=False)
    with patch("services.account_manager.check_account_status_full", AsyncMock()), \
         patch("services.account_manager.should_persist_account_status", lambda *a, **k: False):
        res = _run(op_worker._exec_check_accounts_health(pool, None, 5, 99, {}))
    assert res["status"] == "failed" and "Нет аккаунтов" in res["reason"]
