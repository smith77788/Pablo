"""Массовое вступление: весь флот, а не «лишь часть», и без ложных ошибок.

Жалоба владельца (05.10.2026, операции 236–238): «Вступление в канал × 52 акк.»
через десять минут показало 5 из 50 и только ошибки «Telegram did not return
joined chat», а следом «Назначение админов» дало 9 из 51.

Причины:
  • аккаунты вступали строго по одному, и после ЕДИНСТВЕННОЙ ссылки каждый
    ещё ждал паузу темпа плюс паузу смены аккаунта — около двух минут на
    аккаунт, два часа на флот;
  • Telegram отвечает пустым ответом, когда аккаунт уже в чате, и это
    считалось ошибкой — готовый аккаунт выпадал из выдачи прав и инвайта;
  • «уже состоит в чате» исполнитель тоже писал в ошибки.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services import account_manager
from services import op_worker
from tests.test_bulk_join_idempotent_retry import _Pool, _no_pacing


def _accounts(n):
    return [{"id": i, "phone": f"+7000000000{i}", "session_str": f"s{i}"}
            for i in range(1, n + 1)]


async def _not_quarantined(pool, acc_id):
    return False


@pytest.mark.asyncio
async def test_fleet_joins_in_parallel(monkeypatch):
    monkeypatch.setenv("BULK_JOIN_PARALLEL", "4")
    active = {"now": 0, "peak": 0}
    gate = asyncio.Event()

    async def _join(sess, link, _acc=None):
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
        if active["now"] >= 3:
            gate.set()
        await gate.wait()
        active["now"] -= 1
        return {"channel_id": 1}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)

    res = await asyncio.wait_for(op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555, {"links": ["@chan"], "delay_mode": "smart"},
        _accounts(8)), timeout=5)
    assert active["peak"] >= 3, "аккаунты вступают по одному — флот идёт часами"
    assert active["peak"] <= 4, "параллельность выше заданного потолка"
    assert res["ok"] == 8 and res["failed"] == 0


@pytest.mark.asyncio
async def test_no_pacing_pause_after_accounts_last_link(monkeypatch):
    slept: list = []

    async def _sleep(sec, *a, **k):
        slept.append(sec)

    async def _join(sess, link, _acc=None):
        return {"channel_id": 1}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _sleep)

    await op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555, {"links": ["@chan"], "delay_mode": "smart"},
        _accounts(1))
    assert slept == [], f"после единственной ссылки аккаунт ждал {slept} с впустую"


@pytest.mark.asyncio
async def test_already_member_counts_as_joined(monkeypatch):
    async def _join(sess, link, _acc=None):
        return {"error": "Аккаунт уже состоит в этом чате.", "already_member": True}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)
    res = await op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555, {"links": ["@chan"]}, _accounts(2))
    assert res["ok"] == 2 and res["failed"] == 0


class _Client:
    def __init__(self, left):
        self.left = left

    async def __call__(self, req):
        return SimpleNamespace(chats=[], updates=[], users=[])

    async def get_entity(self, ref):
        return SimpleNamespace(id=5, title="Доставка", username="chan",
                               access_hash=9, left=self.left, megagroup=False,
                               participants_count=10)

    async def disconnect(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("left,ok", [(False, True), (True, False)])
async def test_empty_join_answer_is_checked_against_the_chat(monkeypatch, left, ok):
    async def _connect(*a, **k):
        return _Client(left)
    monkeypatch.setattr(account_manager, "connect_client", _connect)
    res = await account_manager.join_channel("s", "@chan", _acc={"id": 1})
    if ok:
        assert res.get("channel_id") == 5 and not res.get("error"), res
    else:
        assert res.get("error") and "Telegram did not" not in res["error"], res


@pytest.mark.asyncio
async def test_no_long_account_switch_pause(monkeypatch):
    """Пауза «смены аккаунта» 30–90 с шла после каждого — полчаса на флот."""
    slept: list = []

    async def _sleep(sec, *a, **k):
        slept.append(sec)

    async def _join(sess, link, _acc=None):
        return {"channel_id": 1}

    monkeypatch.setattr(account_manager, "join_channel", _join)
    monkeypatch.setattr(op_worker._infra_mem, "is_account_quarantined", _not_quarantined)
    _no_pacing(monkeypatch)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    await op_worker._exec_bulk_join_inner(
        _Pool(), object(), 77, 555, {"links": ["@chan"]}, _accounts(6))
    assert slept and max(slept) <= 15, f"паузы между аккаунтами: {slept}"


class _DialogClient:
    def __init__(self, chan_id):
        self.chan_id = chan_id

    async def get_entity(self, peer):
        raise ValueError("Could not find the input entity")

    async def iter_dialogs(self, limit=None):
        for i in (1, self.chan_id):
            yield SimpleNamespace(entity=SimpleNamespace(id=i, title=f"ch{i}"))


@pytest.mark.asyncio
async def test_own_list_channel_by_id_found_through_dialogs():
    """Канал из своего списка без @username: пустой кэш сессии — ищем в диалогах."""
    from services import mass_inviter_engine as mie
    ent = await mie._resolve_group_entity(_DialogClient(2959208816), "-1002959208816")
    assert ent.id == 2959208816
    with pytest.raises(ValueError):
        await mie._resolve_group_entity(_DialogClient(5), "2959208816")
