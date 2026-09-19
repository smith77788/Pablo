"""Закалка резолва в инвайтинге: числовой id пользователя и проброс флуда с резолва.

Две недоработки того же класса, что и кэш/числовой id канала:

1) parse_user_refs отдаёт числовой id пользователя СТРОКОЙ ('123456789'), а
   client.get_entity по строке из одних цифр падает (username обязан начинаться
   с буквы) — цель не резолвится вовсе. По int Telethon хотя бы заглянет в кэш
   сессии. _coerce_user_ref приводит строку-число к int, остальное — как есть.

2) FloodWait во время резолва группы уходил в общий except как «connect»-ошибка
   с flood_wait=0. Исполнитель не видел флуда, ретайрил аккаунт как «не смог
   подключиться» и НЕ считал флуд в стоп-кран флота — весь флот выжигался на
   резолве по одному. Теперь invite_batch пробрасывает flood_wait/peer_flood.

Юнит-тесты (без Postgres): telethon застаблен в conftest.
"""
from __future__ import annotations

import asyncio

from services import mass_inviter_engine as inv


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_coerce_user_ref_numeric_string_to_int():
    assert inv._coerce_user_ref("123456789") == 123456789
    assert isinstance(inv._coerce_user_ref("123456789"), int)
    assert inv._coerce_user_ref("  456  ") == 456          # с пробелами
    assert inv._coerce_user_ref(789) == 789                # уже int — как есть
    assert inv._coerce_user_ref("@someuser") == "@someuser"  # username — как есть
    assert inv._coerce_user_ref("user123") == "user123"    # буквенно-цифровой — как есть
    assert inv._coerce_user_ref("") == ""


class _FakeClient:
    async def disconnect(self):
        return None


def _flood_error(seconds: int):
    from telethon.errors import FloodWaitError
    e = FloodWaitError()
    e.seconds = seconds
    return e


def test_invite_batch_surfaces_resolve_floodwait(monkeypatch):
    """FloodWait на резолве группы обязан вернуться как flood_wait, а не утонуть в
    «connect»-ошибке — иначе стоп-кран флота его не увидит."""
    inv._ENTITY_CACHE.clear()

    async def _connect(session, acc, purpose):
        return _FakeClient()

    async def _resolve(client, ref, acc_id=None):
        raise _flood_error(112)

    import services.account_manager as am
    monkeypatch.setattr(am, "connect_client", _connect)
    monkeypatch.setattr(inv, "_resolve_group_entity", _resolve)

    res = _run(inv.invite_batch("sess", {"id": 1}, "@grp", ["@a", "@b"]))
    assert res["flood_wait"] == 112, res
    assert res["peer_flood"] is False
    assert res["ok"] == 0 and res["failed"] == 0
    assert res["fail_kinds"].get(inv.FAIL_FLOOD)  # флуд отмечен в корзинах


def test_invite_batch_surfaces_resolve_peerflood(monkeypatch):
    """PeerFlood на резолве → peer_flood=True (аккаунт перегрет, в cooldown)."""
    inv._ENTITY_CACHE.clear()

    async def _connect(session, acc, purpose):
        return _FakeClient()

    async def _resolve(client, ref, acc_id=None):
        from telethon.errors import PeerFloodError
        raise PeerFloodError()

    import services.account_manager as am
    monkeypatch.setattr(am, "connect_client", _connect)
    monkeypatch.setattr(inv, "_resolve_group_entity", _resolve)

    res = _run(inv.invite_batch("sess", {"id": 1}, "@grp", ["@a"]))
    assert res["peer_flood"] is True, res
    assert res["flood_wait"] == 0


def test_invite_batch_non_flood_resolve_error_stays_connect(monkeypatch):
    """Не-флуд ошибка резолва НЕ выставляет флуд-сигналы (иначе ложный cooldown)."""
    inv._ENTITY_CACHE.clear()

    async def _connect(session, acc, purpose):
        return _FakeClient()

    async def _resolve(client, ref, acc_id=None):
        raise ValueError("группа недоступна")

    import services.account_manager as am
    monkeypatch.setattr(am, "connect_client", _connect)
    monkeypatch.setattr(inv, "_resolve_group_entity", _resolve)

    res = _run(inv.invite_batch("sess", {"id": 1}, "@grp", ["@a"]))
    assert res["peer_flood"] is False and res["flood_wait"] == 0, res
    assert any("connect" in str(e) for e in res["errors"])
