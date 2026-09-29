"""Сторож зависших операций не обрывает работу, которая идёт прямо сейчас.

ЧТО ЛОМАЛОСЬ. Сторожей зависших операций в продукте три, и только один из них —
в services/account_monitor — не спрашивал, не выполняется ли операция прямо
сейчас. Он брал всё, что висит в 'running' дольше трёх часов, и писал 'failed'.

Арифметика показывает, что попасть он мог ТОЛЬКО по живой работе. Сторож воркера
(op_worker._watchdog_stale) ходит раз в минуту, порог у него 60 минут, и
выполняющиеся операции он исключает. Значит операция, дожившая в 'running' до
третьего часа, прошла мимо него около ста восьмидесяти раз — а это возможно
только если она в списке активных, то есть исправно работает.

Цена высокая. Потолок одного прогона — 6 часов, и массовый инвайт с пейсингом на
часы укладывается в него штатно. Сторож обрывал такую операцию на середине: писал
терминальный статус, из-за чего настоящий итог исполнителя потом не мог
записаться вовсе (защита от перезаписи терминального статуса), и сообщал
владельцу, что операция зависла и нужен ручной перезапуск. Здоровая многочасовая
работа объявлялась сбоем, её результат терялся, а владельца отправляли гонять
флот второй раз.

ЧТО ТЕПЕРЬ. Выполняющиеся операции исключаются, а порог считается от
собственного потолка прогона этого типа плюс запас.
"""
from __future__ import annotations

import datetime as dt

import pytest


class _Pool:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list = []

    async def fetch(self, query, *args):
        if "operation_queue" in query:
            return self.rows
        return []

    async def execute(self, query, *args):
        self.updates.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetchval(self, query, *args):
        return None


def _row(op_id: int, hours: float, op_type: str = "mass_invite"):
    return {
        "id": op_id,
        "owner_id": 5,
        "op_type": op_type,
        "started_at": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours),
    }


@pytest.fixture(autouse=True)
def _silent_notify(monkeypatch):
    from database import db

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(db, "notify_if_enabled", _noop)


@pytest.mark.asyncio
async def test_a_running_operation_is_never_touched(monkeypatch):
    from services import account_monitor, op_worker

    async def _active():
        return frozenset({77})

    monkeypatch.setattr(op_worker, "active_op_ids", _active)
    pool = _Pool([_row(77, hours=5)])

    await account_monitor._recover_stuck_operations(pool, None)

    assert not pool.updates, (
        "сторож оборвал операцию, которая выполняется прямо сейчас: массовый "
        "инвайт с пейсингом на часы объявлялся сбоем, а его итог терялся"
    )


@pytest.mark.asyncio
async def test_an_orphan_row_past_its_own_ceiling_is_failed(monkeypatch):
    from services import account_monitor, op_worker

    async def _active():
        return frozenset()

    monkeypatch.setattr(op_worker, "active_op_ids", _active)
    pool = _Pool([_row(78, hours=9)])

    await account_monitor._recover_stuck_operations(pool, None)

    assert pool.updates, "осиротевшая строка очереди осталась в 'running' навсегда"
    query, args = pool.updates[0]
    assert "status='failed'" in query
    assert "status='running'" in query, (
        "нет защиты от перезаписи: операция, успевшая завершиться сама, была бы "
        "переписана в провал"
    )


@pytest.mark.asyncio
async def test_an_orphan_within_its_ceiling_is_left_alone(monkeypatch):
    """Три часа — это НЕ зависание: потолок одного прогона вшестеро больше."""
    from services import account_monitor, op_worker

    async def _active():
        return frozenset()

    monkeypatch.setattr(op_worker, "active_op_ids", _active)
    pool = _Pool([_row(79, hours=3.5)])

    await account_monitor._recover_stuck_operations(pool, None)

    assert not pool.updates, (
        "операция оборвана раньше собственного потолка прогона — прежний "
        "плоский предел в 3 часа был вдвое меньше разрешённого"
    )


@pytest.mark.asyncio
async def test_the_message_names_the_real_elapsed_time(monkeypatch):
    from services import account_monitor, op_worker

    async def _active():
        return frozenset()

    monkeypatch.setattr(op_worker, "active_op_ids", _active)
    pool = _Pool([_row(80, hours=10)])

    await account_monitor._recover_stuck_operations(pool, None)

    _, args = pool.updates[0]
    assert any("10 ч" in str(a) for a in args), (
        "в причине зашито фиксированное время вместо настоящего — владельцу "
        "сообщают неправду о том, сколько операция работала"
    )


@pytest.mark.asyncio
async def test_an_unreadable_active_list_stops_the_watchdog(monkeypatch):
    """Не знаем, что выполняется — не трогаем ничего."""
    from services import account_monitor, op_worker

    async def _broken():
        raise RuntimeError("воркер недоступен")

    monkeypatch.setattr(op_worker, "active_op_ids", _broken)
    pool = _Pool([_row(81, hours=99)])

    await account_monitor._recover_stuck_operations(pool, None)

    assert not pool.updates, (
        "сторож пометил операции провалившимися, не зная, какие из них живые"
    )


@pytest.mark.asyncio
async def test_the_active_list_has_one_public_way_in():
    from services import op_worker

    ids = await op_worker.active_op_ids()

    assert isinstance(ids, frozenset), (
        "список активных операций должен отдаваться неизменяемым: чужой модуль "
        "не должен иметь возможности его поправить"
    )
