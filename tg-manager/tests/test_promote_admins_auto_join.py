"""Назначение админов: промоутер сам добавляет не-вступивших, а не отказывает.

Жалоба владельца: «Назначение админов» — успешно 9 из 51, 42 ошибки. Причина —
42 аккаунта ещё не состояли в канале (UserNotParticipant), и promote_to_admin_ex
просто отдавал not_participant. Теперь при not_participant промоутер добавляет
аккаунт в канал (InviteToChannel) и повторяет выдачу прав — операция стала
самодостаточной.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import account_manager, op_worker


def test_promote_auto_invites_non_members():
    src = inspect.getsource(account_manager.promote_to_admin_ex)
    i = src.index("except UserNotParticipantError:")
    seg = src[i:i + 900]
    assert "InviteToChannelRequest" in seg, (
        "promote_to_admin_ex не добавляет не-участника перед выдачей прав — "
        "массовое назначение падает not_participant")
    assert "EditAdminRequest" in seg, "после вступления права не выдаются повторно"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Pool:
    def __init__(self, accounts):
        self._accounts = accounts
        self.done = 0

    async def fetch(self, q, *a):
        if "FROM tg_accounts" in q:
            return self._accounts
        if "FROM operation_log" in q:
            return []
        return []

    async def fetchrow(self, q, *a):
        if "FROM tg_accounts" in q and "id=" in q:
            return self._accounts[0]
        return None

    async def execute(self, q, *a):
        if "done_items=done_items+1" in q:
            self.done += 1
        return "OK"


@pytest.fixture
def _stub(monkeypatch):
    async def _claim(acc_id):
        return True
    monkeypatch.setattr(op_worker, "try_claim_account", _claim)

    async def _release(ids):
        return None
    monkeypatch.setattr(op_worker, "release_accounts", _release)

    async def _get_acc(pool, acc_id, owner_id=None):
        return {"id": acc_id, "session_str": "s"}
    from database import db as _db
    monkeypatch.setattr(_db, "get_account_for_telethon", _get_acc)

    async def _cancelled(pool, op_id):
        return False
    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)

    async def _completed(pool, op_id):
        return set()
    monkeypatch.setattr(op_worker, "completed_targets", _completed)

    async def _delay(pool, owner_id, base):
        return 0.0
    monkeypatch.setattr(op_worker, "_governed_delay", _delay)


def test_all_members_promoted(_stub, monkeypatch):
    accounts = [{"id": i, "phone": f"+{i}", "first_name": "A", "tg_user_id": 1000 + i}
                for i in range(1, 6)]
    pool = _Pool(accounts)

    async def _promote(session, chan, uid, **kw):
        return True, ""  # промоутер теперь всегда может (добавит + повысит)
    monkeypatch.setattr(account_manager, "promote_to_admin_ex", _promote)

    res = _run(op_worker._exec_promote_all_admins(
        pool, None, 1, 777, {"owner_acc_id": 99, "channel_id": -100123}))
    assert res["ok"] == 5, res
