"""Смена названия/описания канала должна передавать access_hash и @username.

Баг: edit_channel_title/about резолвили канал по «голому» id на свежем клиенте
(get_entity(id)) — для каналов без кэша сущности это падало, и операция давала
0 изменённых. Фикс: пробрасываем access_hash и username из managed_channels, а
резолвер строит InputPeerChannel / @username. Проверяем, что исполнитель
bulk_chan_exec реально передаёт эти поля в account_manager.
"""
from __future__ import annotations

import asyncio

from services import op_worker


class _Pool:
    async def execute(self, q, *a): return "OK"
    async def fetch(self, q, *a): return []
    async def fetchval(self, q, *a): return 0
    async def fetchrow(self, q, *a): return None


def test_title_edit_forwards_access_hash_and_username(monkeypatch):
    captured = {}

    async def _select_all_active(pool, owner_id, **kw):
        return [{"id": 5, "session_str": "s5", "first_name": "acc"}]
    async def _claim(ids): return list(ids)
    async def _release(ids): return None
    async def _cancelled(pool, op_id): return False
    monkeypatch.setattr(op_worker.resource_selector, "select_all_active", _select_all_active)
    monkeypatch.setattr(op_worker, "try_claim_accounts", _claim)
    monkeypatch.setattr(op_worker, "release_accounts", _release)
    monkeypatch.setattr(op_worker, "_is_cancelled", _cancelled)

    from services import account_manager
    async def _edit_title(session, ch_id, value, _acc=None, access_hash=0, username=""):
        captured.update({"ch_id": ch_id, "value": value,
                         "access_hash": access_hash, "username": username})
        return True
    monkeypatch.setattr(account_manager, "edit_channel_title", _edit_title)

    params = {
        "op": "chan_title", "value": "Новое имя",
        "channel_acc_pairs": [{"channel_id": 111, "acc_id": 5,
                               "title": "Старое", "access_hash": 999, "username": "mychan"}],
    }
    res = asyncio.run(op_worker._exec_bulk_chan_exec(_Pool(), None, 1, 42, params))

    assert captured["access_hash"] == 999, "access_hash обязан дойти до резолвера"
    assert captured["username"] == "mychan", "username обязан дойти до резолвера"
    assert captured["value"] == "Новое имя"
    assert res.get("ok") == 1, "название должно смениться (1 успех), а не 0"
