"""Удаление личных диалогов (раздел 3 «Действие с аккаунтом» паритета TE).

_exec_delete_private_dialogs удаляет ТОЛЬКО личные (is_user) диалоги —
комплемент к leave_all_chats (группы/каналы). Тест на мок-pool и мок-Telethon:
трогает только ЛС, счётчики, прогресс, отмена, регистрация в диспетчере/action.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from services import op_worker



class _Dialog:
    def __init__(self, id, is_user, entity):
        self.id = id
        self.is_user = is_user
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


def test_deletes_only_private_dialogs():
    acc = {"id": 1, "session_str": "sess", "proxy_url": ""}
    pool = _FakePool(acc)

    pm1, grp, pm2 = object(), object(), object()
    dialogs = [_Dialog(1, True, pm1), _Dialog(2, False, grp), _Dialog(3, True, pm2)]

    client = AsyncMock()
    client.connect = AsyncMock()
    client.get_dialogs = AsyncMock(return_value=dialogs)
    client.delete_dialog = AsyncMock()
    client.disconnect = AsyncMock()

    with patch("services.account_manager._make_client", return_value=client), \
         patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)), \
         patch("asyncio.sleep", new=AsyncMock()):
        res = _run(op_worker._exec_delete_private_dialogs(pool, None, 7, 99, {"account_id": 1}))

    assert res["status"] == "done" and res["deleted"] == 2 and res["failed"] == 0
    deleted = [c.args[0] for c in client.delete_dialog.call_args_list]
    assert pm1 in deleted and pm2 in deleted and grp not in deleted  # группу не трогаем
    assert any("total_items" in q for q, _ in pool.executed)


def test_no_account_id():
    res = _run(op_worker._exec_delete_private_dialogs(_FakePool(None), None, 1, 1, {}))
    assert res["status"] == "failed"


def test_account_missing():
    res = _run(op_worker._exec_delete_private_dialogs(_FakePool(None), None, 1, 1, {"account_id": 9}))
    assert res["status"] == "failed"


def test_wired_in_dispatch_and_action():
    import inspect
    assert op_worker.handler_for("delete_private_dialogs") is not None
    from services import mini_app_api
    msrc = inspect.getsource(mini_app_api)
    assert '"delete_private_dialogs"' in msrc and 'act == "delete_pm"' in msrc
