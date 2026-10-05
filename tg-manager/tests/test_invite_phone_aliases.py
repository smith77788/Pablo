"""Номер телефона — лишь один из адресов человека.

Дыра: приглашённый по номеру записывался в журнал только номером. Тот же
человек в очереди по @username (аудитория собрана из двух источников) или в
следующей кампании получал второе приглашение — и наоборот: приглашённого по
@username номер звал снова. Короткая пауза Telegram на номере сразу списывала
его в отказы.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError  # стаб-класс из conftest

from services import account_manager
from services import mass_inviter_engine as mie
from tests.test_invite_queue_scheduler import (  # noqa: F401 — фикстуры стенда
    _Pool,
    _reset_account_claims,
    _run,
    stand,
)


def _flood(seconds: int) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class _PhoneClient:
    """Первый вызов — импорт контактов, дальше — приглашения по очереди исходов."""

    def __init__(self, users, effects=()):
        self.users = users
        self.effects = list(effects)
        self._ok_calls = 0
        self._n = 0

    @property
    def invites(self) -> int:
        return self._ok_calls - 1   # последний успешный вызов — удаление контактов

    async def __call__(self, req):
        self._n += 1
        if self._n == 1:
            return SimpleNamespace(
                users=[u for _, u in self.users],
                imported=[SimpleNamespace(client_id=i, user_id=u.id)
                          for i, (_, u) in enumerate(self.users)])
        eff = self.effects.pop(0) if self.effects else None
        if eff is not None:
            raise eff
        self._ok_calls += 1
        return None

    async def disconnect(self):
        return None


@pytest.fixture
def engine(monkeypatch):
    def _install(client):
        async def _connect(*a, **k):
            return client

        async def _group(c, ref, acc_id=None):
            return "GRP"

        async def _fast(*a, **k):
            return None

        monkeypatch.setattr(account_manager, "connect_client", _connect)
        monkeypatch.setattr(mie, "_resolve_group_entity", _group)
        monkeypatch.setattr(asyncio, "sleep", _fast)
        return client
    return _install


IVAN = SimpleNamespace(id=111, username="Ivan")
OLGA = SimpleNamespace(id=222, username=None)


@pytest.mark.asyncio
async def test_invited_by_phone_is_reported_under_username_and_id(engine):
    engine(_PhoneClient([("+7900", IVAN), ("+7901", OLGA)]))
    res = await mie.invite_by_phones("s", {"id": 1}, "@g", ["+7900", "+7901"])
    assert res["invited_phones"] == ["+7900", "+7901"]
    assert set(res["invited_aliases"]) == {"@Ivan", "111", "222"}


@pytest.mark.asyncio
async def test_phone_of_already_invited_username_is_not_invited(engine):
    c = engine(_PhoneClient([("+7900", IVAN), ("+7901", OLGA)]))
    res = await mie.invite_by_phones("s", {"id": 1}, "@g", ["+7900", "+7901"],
                                     skip_keys={"@ivan"})
    assert c.invites == 1, "Иван уже приглашён по @username — номер его не зовёт"
    assert res["already_phones"] == ["+7900"]
    assert res["invited_phones"] == ["+7901"]


@pytest.mark.asyncio
async def test_short_flood_on_phone_retries_the_same_number(engine):
    c = engine(_PhoneClient([("+7900", IVAN)], effects=[_flood(10)]))
    res = await mie.invite_by_phones("s", {"id": 1}, "@g", ["+7900"])
    assert c.invites == 1 and res["ok"] == 1 and res["failed"] == 0
    assert res["short_floods"] == [10]


def test_executor_records_phone_invitee_under_all_addresses(stand, monkeypatch):
    from services import op_worker

    recorded: set = set()

    async def _rec(pool, owner_id, key, op_id, targets):
        recorded.update(str(t) for t in targets)
        return True
    monkeypatch.setattr(op_worker, "_record_invited_targets", _rec)

    s = stand(lambda acc_id, refs, dry=False: {"ok": len(refs), "failed": 0,
                                               "errors": [], "untried": []})
    seen_skip: list = []

    async def _phones(sess, acc, group, phones, skip_keys=None):
        seen_skip.append(set(skip_keys or ()))
        return {"ok": len(phones), "failed": 0, "errors": [], "untried": [],
                "invited_phones": list(phones), "invited_aliases": ["@Ivan", "111"],
                "already_phones": []}
    monkeypatch.setattr(mie, "invite_by_phones", _phones)

    _run(_Pool(), ["u1"], phones=["+79001234567"])
    assert {"@Ivan", "111", "+79001234567"} <= recorded
    assert seen_skip and all(isinstance(x, set) for x in seen_skip)
    del s
