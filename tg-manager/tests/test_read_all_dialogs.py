"""Чтение диалогов (раздел 3 «Действие с аккаунтом» паритета TE).

_exec_read_all_dialogs помечает ТОЛЬКО непрочитанные диалоги прочитанными
(send_read_acknowledge) — реальное «дочитывание», не заглушка. Тест на мок-pool
и мок-Telethon: считает прочитанные, трогает только unread, обновляет прогресс.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker


class _Dialog:
    def __init__(self, id, unread, entity):
        self.id = id
        self.unread_count = unread
        self.entity = entity


class _FakePool:
    def __init__(self, acc):
        self._acc = acc
        self.executed = []

    async def fetchrow(self, q, *a):
        return self._acc

    async def execute(self, q, *a):
        self.executed.append((q, a))
        return "OK"


def _run(coro):
    return asyncio.run(coro)


def test_marks_only_unread_dialogs_read():
    acc = {"id": 1, "session_str": "sess", "proxy_url": ""}
    pool = _FakePool(acc)

    e1, e2, e3 = object(), object(), object()
    dialogs = [_Dialog(1, 3, e1), _Dialog(2, 0, e2), _Dialog(3, 5, e3)]

    client = AsyncMock()
    client.connect = AsyncMock()
    client.get_dialogs = AsyncMock(return_value=dialogs)
    client.send_read_acknowledge = AsyncMock()
    client.disconnect = AsyncMock()

    with patch("services.account_manager._make_client", return_value=client), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_read_all_dialogs(pool, None, 55, 99, {"account_id": 1}))

    assert res["status"] == "done" and res["read"] == 2 and res["failed"] == 0
    # прочитаны именно два непрочитанных
    called = [c.args[0] for c in client.send_read_acknowledge.call_args_list]
    assert e1 in called and e3 in called and e2 not in called
    # total_items выставлен в 2
    assert any("total_items" in q for q, _ in pool.executed)


def test_no_account_id():
    pool = _FakePool(None)
    res = _run(op_worker._exec_read_all_dialogs(pool, None, 1, 1, {}))
    assert res["status"] == "failed"


def test_account_missing():
    pool = _FakePool(None)
    res = _run(op_worker._exec_read_all_dialogs(pool, None, 1, 1, {"account_id": 9}))
    assert res["status"] == "failed"


def test_wired_in_dispatch_and_action():
    import inspect
    src = inspect.getsource(op_worker)
    assert 'op_type == "read_all_dialogs"' in src
    from services import mini_app_api
    msrc = inspect.getsource(mini_app_api)
    assert '"read_all_dialogs"' in msrc and 'act == "read_all"' in msrc
