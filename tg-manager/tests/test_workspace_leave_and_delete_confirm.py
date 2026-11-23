"""Выход из пространства и его удаление — через подтверждение.

Было: «🚪 Покинуть» выводило из пространства с первого нажатия, а владелец
под кнопкой «🗑 Удалить» получал «владелец не может покинуть» — удаления в
боте не было вовсе, кнопка обещала то, чего нет.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.callbacks import WorkspaceCb
from bot.handlers import workspaces as ws


def _cb(uid=42):
    cb = MagicMock()
    cb.from_user.id = uid
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


def _buttons(cb):
    return [b.callback_data for row in cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row]


def test_member_leave_asks_first_and_does_not_leave():
    cb = _cb()
    with patch.object(ws.db, "get_workspace", AsyncMock(return_value={"owner_id": 7, "name": "Команда"})), \
         patch.object(ws.db, "delete_workspace_member", AsyncMock()) as leave:
        asyncio.run(ws.cb_ws_leave(cb, WorkspaceCb(action="leave", ws_id=5), MagicMock()))
    leave.assert_not_awaited()
    assert "Покинуть пространство «Команда»?" in cb.message.edit_text.await_args.args[0]
    assert WorkspaceCb(action="leave_yes", ws_id=5).pack() in _buttons(cb)


def test_owner_gets_a_real_delete_confirmation():
    cb = _cb(uid=7)
    members = [{"user_id": 7}, {"user_id": 8}, {"user_id": 9}]
    with patch.object(ws.db, "get_workspace", AsyncMock(return_value={"owner_id": 7, "name": "Команда"})), \
         patch.object(ws.db, "get_workspace_members", AsyncMock(return_value=members)):
        asyncio.run(ws.cb_ws_leave(cb, WorkspaceCb(action="leave", ws_id=5), MagicMock()))
    text = cb.message.edit_text.await_args.args[0]
    assert "Удалить пространство «Команда»?" in text and "2 участника потеряют" in text
    assert "не может покинуть" not in text
    assert WorkspaceCb(action="delete_yes", ws_id=5).pack() in _buttons(cb)


def test_confirmed_leave_leaves():
    cb = _cb()
    with patch.object(ws.db, "get_workspace", AsyncMock(return_value={"owner_id": 7, "name": "К"})), \
         patch.object(ws.db, "delete_workspace_member", AsyncMock()) as leave:
        asyncio.run(ws.cb_ws_leave_yes(cb, WorkspaceCb(action="leave_yes", ws_id=5), MagicMock()))
    leave.assert_awaited_once()
    assert "покинули" in cb.message.edit_text.await_args.args[0]


def test_confirmed_delete_deletes_by_owner():
    cb = _cb(uid=7)
    with patch.object(ws.db, "delete_workspace", AsyncMock(return_value=True)) as dele:
        asyncio.run(ws.cb_ws_delete_yes(cb, WorkspaceCb(action="delete_yes", ws_id=5), MagicMock()))
    assert dele.await_args.args[1:] == (5, 7)
    assert "удалено" in cb.message.edit_text.await_args.args[0]


class _Pool:
    def __init__(self, deleted):
        self.deleted = deleted
        self.audit = []
        self.q = None

    async def fetchval(self, q, *a):
        self.q = (q, a)
        return self.deleted

    async def execute(self, q, *a):
        if "INSERT INTO operation_audit" in q:
            self.audit.append(a)
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_delete_workspace_is_owner_scoped_and_logged():
    from database import db
    pool = _Pool(deleted=5)
    assert await db.delete_workspace(pool, 5, 7) is True
    q, args = pool.q
    assert "owner_id=$2" in q and args == (5, 7)
    assert pool.audit and pool.audit[0][1] == "workspace_delete"


@pytest.mark.asyncio
async def test_nothing_deleted_nothing_logged():
    from database import db
    pool = _Pool(deleted=None)
    assert await db.delete_workspace(pool, 5, 8) is False
    assert pool.audit == []
