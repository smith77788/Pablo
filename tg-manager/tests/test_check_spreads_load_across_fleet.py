"""Проверка ограничений РАЗМАЗЫВАЕТ нагрузку по флоту, а не грузит 1–2 аккаунта.

Жалоба владельца: «для проверки всех сущностей берутся лишь 1–2 аккаунта из
флота, которые после получают спамблок и другие ограничения и сниженный траст».
Причина была: публичную сущность резолвил её управляющий аккаунт (а один
аккаунт управляет сотнями каналов), и один первый аккаунт искал ВСЕ. Теперь —
round-robin по ограниченному пулу наблюдателей: каждая сущность с другого
аккаунта, нагрузка на аккаунт держится ниже безопасного порога.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
from unittest.mock import AsyncMock, patch

from services import op_worker
from services import entity_restriction_check as erc


class _FakePool:
    def __init__(self, channels, accounts):
        self._channels = channels
        self._accounts = accounts

    async def fetch(self, query, *a):
        q = query.lower()
        if "managed_channels" in q:
            return self._channels
        if "managed_bots" in q:
            return []
        if "tg_accounts" in q:
            return self._accounts
        return []

    async def execute(self, query, *a):
        return "UPDATE 1"

    async def fetchval(self, query, *a):
        return 0


class _TaggedClient:
    def __init__(self, acc_id):
        self.acc_id = acc_id

    async def disconnect(self):
        return None

    async def get_dialogs(self, limit=None):
        return []


def _ch(cid, username):
    # acc_id=None: публичная без управляющего — чистый round-robin резолва.
    return {"channel_id": cid, "username": username, "title": "C" + str(cid),
            "type": "channel", "acc_id": None}


def _acc(i):
    return {"id": i, "session_str": "sess", "first_name": "A" + str(i),
            "phone": "+1", "username": "u", "proxy_url": None}


def _clean(status="clean"):
    return {"status": status, "label": erc.status_label(status),
            "reason_ru": "ok", "signals": {}, "meta": {"title": "X"}}


def test_resolve_load_is_spread_not_concentrated():
    n = 120
    channels = [_ch(1000 + i, "pub%d" % i) for i in range(n)]
    accounts = [_acc(i) for i in range(1, 9)]  # флот из 8 аккаунтов

    resolves = collections.Counter()   # acc_id -> сколько сущностей он резолвил

    def _connect(session_str, device=None, **kw):
        return _TaggedClient(int((device or {}).get("id")))

    async def _probe(client, **kw):
        resolves[client.acc_id] += 1
        return _clean()

    patches = [
        patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)),
        patch("services.infra_memory.is_account_quarantined", AsyncMock(return_value=False)),
        patch("services.account_manager.connect_client", AsyncMock(side_effect=_connect)),
        patch("services.entity_restriction_check.probe_entity", AsyncMock(side_effect=_probe)),
        patch("services.shadowban_monitor._record_event", AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        res = asyncio.run(op_worker._exec_check_owned_restrictions(
            _FakePool(channels, accounts), None, 7, 99, {}))

    assert res["status"] == "done" and res["checked"] == n
    # Нагрузку несёт НЕ один аккаунт: задействовано заметно больше одного.
    assert len(resolves) >= 3, ("резолв всё ещё концентрируется на горстке: "
                                f"{dict(resolves)}")
    # И ни один аккаунт не тянет всё: самый занятый — явно меньше всего набора.
    busiest = max(resolves.values())
    assert busiest <= n // 2, (f"один аккаунт сделал {busiest} из {n} резолвов — "
                               "нагрузка не размазана")
    # Все пробы учтены (на каждую сущность — ровно один успешный резолв).
    assert sum(resolves.values()) == n


def test_pool_leaves_fleet_reserve_on_small_sets():
    """На небольшом наборе пул маленький — не забирает весь флот под проверку."""
    channels = [_ch(2000 + i, "p%d" % i) for i in range(10)]  # мало сущностей
    accounts = [_acc(i) for i in range(1, 21)]  # большой флот (20)

    used = set()

    def _connect(session_str, device=None, **kw):
        used.add(int((device or {}).get("id")))
        return _TaggedClient(int((device or {}).get("id")))

    async def _probe(client, **kw):
        return _clean()

    patches = [
        patch.object(op_worker, "_is_cancelled", AsyncMock(return_value=False)),
        patch("services.infra_memory.is_account_quarantined", AsyncMock(return_value=False)),
        patch("services.account_manager.connect_client", AsyncMock(side_effect=_connect)),
        patch("services.entity_restriction_check.probe_entity", AsyncMock(side_effect=_probe)),
        patch("services.shadowban_monitor._record_event", AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        asyncio.run(op_worker._exec_check_owned_restrictions(
            _FakePool(channels, accounts), None, 7, 99, {}))

    # 10 сущностей на пороге 40 → пул 2; из 20 аккаунтов проверка трогает горстку,
    # остальной флот свободен для действий владельца.
    assert len(used) <= 4, f"проверка задействовала слишком много флота: {used}"
