"""Регресс: массовая проверка считает временный и вечный спам-блок раздельно.

check_account_status_full теперь отдаёт spamblock_kind ('temp'|'perm').
_exec_check_accounts_health должен разложить их в отдельные счётчики
(spamblock_temp/spamblock_perm) и показать разбивку в summary — это питает
раздельные счётчики/папки «Временный/Вечный спамблок» в панели. Общий статус
остаётся 'spamblock' (контракт цел).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker


class _FakePool:
    def __init__(self, accounts):
        self._accounts = accounts

    async def fetch(self, query, *a):
        return self._accounts

    async def execute(self, query, *a):
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


def _run(coro):
    return asyncio.run(coro)


def _acc(i):
    return {"id": i, "session_str": "s", "first_name": f"A{i}",
            "phone": "+1", "username": "u", "proxy_url": None}


def test_temp_and_perm_spamblock_counted_separately():
    results = [
        {"status": "spamblock", "spamblock_kind": "temp", "reason": "x"},
        {"status": "spamblock", "spamblock_kind": "perm", "reason": "x"},
        {"status": "spamblock", "spamblock_kind": "perm", "reason": "x"},
        {"status": "active", "reason": "ok"},
    ]
    with patch("services.account_manager.check_account_status_full",
               AsyncMock(side_effect=results)), \
         patch("services.account_manager.should_persist_account_status",
               lambda *a, **k: False), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_check_accounts_health(
            _FakePool([_acc(1), _acc(2), _acc(3), _acc(4)]),
            None, 5, 99, {"check_spambot": True}))

    assert res["status"] == "done"
    assert res["spamblock_temp"] == 1
    assert res["spamblock_perm"] == 2
    assert res["status_counts"].get("spamblock") == 3
    assert "врем.: 1, вечн.: 2" in res["summary"]


def test_spamblock_without_kind_defaults_to_perm_count():
    # Старый путь без spamblock_kind (напр. фолбэк) — считаем как вечный,
    # чтобы аккаунт не попал ошибочно в «временные» и авто-ретраи.
    results = [{"status": "spamblock", "reason": "x"}]
    with patch("services.account_manager.check_account_status_full",
               AsyncMock(side_effect=results)), \
         patch("services.account_manager.should_persist_account_status",
               lambda *a, **k: False), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_check_accounts_health(
            _FakePool([_acc(1)]), None, 5, 99, {"check_spambot": True}))

    assert res["spamblock_temp"] == 0
    assert res["spamblock_perm"] == 1
