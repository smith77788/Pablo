"""Массовый инвайт — регрессия: длинный FloodWait не должен продолжать инвайты.

invite_batch на FloodWait спал min(seconds, 60) и ПРОДОЛЖАЛ инвайтить — при
длинном флуде это слать запросы во время активного flood-wait = эскалация
(Telegram усиливает ограничение). Плюс исполнитель не ставил cooldown флагнутому
аккаунту. Теперь длинный флуд прерывает батч и сигналит наверх (flood_wait),
а _exec_mass_invite ставит cooldown через единый flood-сигнал.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, patch

import pytest

from telethon.errors import FloodWaitError  # стаб-класс из conftest
from services import mass_inviter_engine as inv


def _flood(seconds: int) -> FloodWaitError:
    e = FloodWaitError("flood")
    e.seconds = seconds
    return e


class _FakeClient:
    def __init__(self, effects):
        self._effects = list(effects)
        self._i = 0
        self.disconnect = AsyncMock()

    async def connect(self):
        return None

    def is_connected(self):
        return True

    async def get_entity(self, ref):
        return object()

    async def __call__(self, req):
        eff = self._effects[self._i]
        self._i += 1
        if isinstance(eff, Exception):
            raise eff
        return eff


async def _run(effects, refs):
    client = _FakeClient(effects)
    with patch("services.account_manager._make_client", return_value=client), \
         patch.object(inv, "_resolve_group_entity", AsyncMock(return_value=object())), \
         patch.object(inv.asyncio, "sleep", AsyncMock()):
        # bulk=False закрепляет ПРЕДМЕТ этих проверок — поштучный путь, где у
        # каждой цели свой исход. В пакетном режиме весь батч уходит одним
        # запросом, и после пережидания короткого флуда добавляются оба: это не
        # регрессия, а другая семантика, и она проверяется в
        # tests/test_invite_bulk_api.py.
        return await inv.invite_batch("sess", {"id": 1}, "@group", refs, bulk=False)


@pytest.mark.asyncio
async def test_long_floodwait_stops_and_signals():
    res = await _run([_flood(300)], ["@u1", "@u2", "@u3"])
    assert res["flood_wait"] == 300          # сигнал наверх
    assert res["ok"] == 0
    # прервались на первом — второго/третьего не трогали (не инвайтим во время флуда)


@pytest.mark.asyncio
async def test_short_floodwait_continues():
    # u1 — короткий флуд (переживаем), u2 — успех
    res = await _run([_flood(20), object()], ["@u1", "@u2"])
    assert res["flood_wait"] == 0
    assert res["ok"] == 1


@pytest.mark.asyncio
async def test_return_dict_has_flood_wait_key():
    res = await _run([object()], ["@u1"])
    assert "flood_wait" in res and res["flood_wait"] == 0


def test_executor_applies_cooldown_on_flag():
    """_exec_mass_invite должен ставить cooldown флагнутому/зафлуженному аккаунту."""
    from services import op_worker

    src = inspect.getsource(op_worker._exec_mass_invite)
    assert "_rest_invite_account" in src
    assert "record_peer_flood" in src
    assert 'res.get("flood_wait")' in src  # длинный флуд тоже ведёт к cooldown
