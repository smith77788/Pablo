"""Чужие операции провалившимися помечает только держатель аренды.

ИСТОРИЯ. У сторожа зависших операций в `account_monitor` уже была эта поломка:
он помечал провалившейся операцию, которая в ту же секунду исправно работала.
Починили доступом к `op_worker.active_op_ids()` — публичному виду на список
выполняющихся операций (так и написано в докстринге того списка).

ЧТО ОСТАЛОСЬ. Этот список — ПАМЯТЬ ОДНОГО ПРОЦЕССА. Очередь разбирает один
процесс (аренда исполнителя), но `account_monitor.run` под арендой НЕ стоит:
это отдельный фоновый цикл, и в процессе, который операций не берёт,
`active_op_ids()` пуст по определению. Значит второй процесс с ролью
worker/all видит живые операции держателя как осиротевшие и закрывает их
'failed' — с докладом владельцу о провале работы, которая идёт прямо сейчас.
Ровно то, от чего аренда и защищает: в докстринге `replica_guard` сказано, что
сторожа «не отличают свои операции от чужих».

ПОЧЕМУ ЧТЕНИЕ, А НЕ ВЗЯТИЕ. Проверять надо `executor_lease_holder` (чтение), а
не `acquire_executor_lease`: взятие продлевает аренду и в чужом процессе могло
бы её у держателя отобрать. Не прочитали — не трогаем ничего, как и при
недоступном списке активных операций.
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import pytest


class _Pool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *a):
        if "operation_queue" in sql:
            return list(self._rows)
        return []

    async def execute(self, *a, **kw):
        return "UPDATE 1"

    async def fetchrow(self, *a, **kw):
        return None


def _old_row(op_id=77, hours=10):
    return {
        "id": op_id,
        "owner_id": 500,
        "op_type": "mass_publish",
        "started_at": _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours),
    }


@pytest.fixture
def stage(monkeypatch):
    """Стенд: операция висит десять часов, в ЭТОМ процессе активных нет."""
    from services import account_monitor as am
    from services import op_worker as ow
    from services import replica_guard as rg

    closed: list[int] = []

    async def _no_active():
        return frozenset()

    async def _close(pool, bot, op_id, row, hours):
        closed.append(int(op_id))

    monkeypatch.setattr(ow, "active_op_ids", _no_active)
    monkeypatch.setattr(am, "_close_orphan_op", _close)
    return am, rg, ow, closed


def test_a_process_without_the_lease_does_not_close_them(stage, monkeypatch):
    """Аренда у другого процесса — его живые операции не наши, не трогаем."""
    am, rg, _ow, closed = stage

    async def _holder(pool):
        return "другой-процесс"

    monkeypatch.setattr(rg, "executor_lease_holder", _holder)
    asyncio.run(am._recover_stuck_operations(_Pool([_old_row()]), None))
    assert closed == [], (
        "процесс, не держащий аренду исполнителя, закрыл операцию как "
        "осиротевшую — а список активных операций у него пуст просто потому, "
        "что операции идут в другом процессе. Владелец получит «провалилась» "
        "по работе, которая идёт прямо сейчас"
    )


def test_the_holder_still_closes_them(stage, monkeypatch):
    """Самопроверка: у держателя страховка работает как раньше."""
    am, rg, ow, closed = stage

    async def _holder(pool):
        return ow._WORKER_ID

    monkeypatch.setattr(rg, "executor_lease_holder", _holder)
    asyncio.run(am._recover_stuck_operations(_Pool([_old_row()]), None))
    assert closed == [77], (
        "держатель аренды перестал закрывать осиротевшие операции — страховка "
        "на строки, которые не закрыл никто, пропала"
    )


def test_an_unreadable_lease_touches_nothing(stage, monkeypatch):
    """Не прочитали аренду — ничего не делаем (как и при недоступном active)."""
    am, rg, _ow, closed = stage

    async def _holder(pool):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(rg, "executor_lease_holder", _holder)
    asyncio.run(am._recover_stuck_operations(_Pool([_old_row()]), None))
    assert closed == [], "аренду прочитать не удалось, а операции всё равно закрыли"


def test_an_empty_lease_row_touches_nothing(stage, monkeypatch):
    """Аренды нет ни у кого (None) — тоже не наше дело."""
    am, rg, _ow, closed = stage

    async def _holder(pool):
        return None

    monkeypatch.setattr(rg, "executor_lease_holder", _holder)
    asyncio.run(am._recover_stuck_operations(_Pool([_old_row()]), None))
    assert closed == []


def test_active_operations_are_still_spared(stage, monkeypatch):
    """Самопроверка прежней защиты: своя выполняющаяся операция не закрывается."""
    am, rg, ow, closed = stage

    async def _holder(pool):
        return ow._WORKER_ID

    async def _active():
        return frozenset({77})

    monkeypatch.setattr(rg, "executor_lease_holder", _holder)
    monkeypatch.setattr(ow, "active_op_ids", _active)
    asyncio.run(am._recover_stuck_operations(_Pool([_old_row()]), None))
    assert closed == []
