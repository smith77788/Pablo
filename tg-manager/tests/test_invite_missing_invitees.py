"""Честный успех инвайта: пользователь в missing_invitees НЕ засчитывается как ok.

Современный Telegram не бросает UserPrivacyRestricted, а тихо возвращает
не-добавленных в поле missing_invitees ответа InviteToChannel. invite_batch должен
считать таких провалом (и класть в privacy_failed для фолбэка), а не успехом.
"""
from __future__ import annotations

import asyncio

import pytest

from services import mass_inviter_engine as mie
from services import account_manager


class _FakeClient:
    """Мини-клиент: get_entity запоминает текущий ref, вызов InviteToChannel
    возвращает объект с missing_invitees, если этот ref «приватный»."""

    def __init__(self, missing_for):
        self.missing_for = set(missing_for)
        self._last = None

    async def get_entity(self, ref):
        self._last = ref
        return ref

    async def __call__(self, request):
        class _R:
            pass
        r = _R()
        r.missing_invitees = [object()] if self._last in self.missing_for else []
        return r

    async def disconnect(self):
        return None


@pytest.mark.asyncio
async def test_missing_invitee_is_failure_not_success(monkeypatch):
    fake = _FakeClient(missing_for={"@bob"})

    async def _connect(*a, **k):
        return fake

    async def _group(client, ref, acc_id=None):
        return "GRP"

    async def _fast(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "connect_client", _connect)
    monkeypatch.setattr(mie, "_resolve_group_entity", _group)
    monkeypatch.setattr(asyncio, "sleep", _fast)

    # bulk=False: проверяем поштучный путь (у пакетного своя атрибуция
    # по missing_invitees — tests/test_invite_bulk_api.py)
    res = await mie.invite_batch("sess", {"id": 1}, "@grp", ["@alice", "@bob"],
                                 bulk=False)
    # alice добавлена (missing пуст), bob — в missing_invitees → провал
    assert res["ok"] == 1
    assert res["failed"] == 1
    assert "@bob" in res["privacy_failed"]
    assert "@alice" not in res["privacy_failed"]


@pytest.mark.asyncio
async def test_all_added_when_no_missing(monkeypatch):
    fake = _FakeClient(missing_for=set())

    async def _connect(*a, **k):
        return fake

    async def _group(client, ref, acc_id=None):
        return "GRP"

    async def _fast(*a, **k):
        return None

    monkeypatch.setattr(account_manager, "connect_client", _connect)
    monkeypatch.setattr(mie, "_resolve_group_entity", _group)
    monkeypatch.setattr(asyncio, "sleep", _fast)

    res = await mie.invite_batch("sess", {"id": 1}, "@grp", ["@a", "@b", "@c"],
                                 bulk=False)
    assert res["ok"] == 3
    assert res["failed"] == 0
    assert res["privacy_failed"] == []
