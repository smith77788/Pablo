"""Гейт подтверждения не должен падать ОТКРЫТЫМ.

МЕХАНИКА. `enqueue_op_with_approval` ставит операцию двумя шагами: сначала
`operation_bus.submit` кладёт её в очередь со статусом 'pending', затем второй
UPDATE переводит крупную операцию в 'waiting_approval'. Докстринг объясняет
порядок: «воркер берёт из очереди только 'pending', поэтому до подтверждения
она не исполняется».

ЧТО БЫЛО НЕ ТАК. Ровно из этого порядка и следует дыра: между двумя шагами
операция УЖЕ в очереди и УЖЕ 'pending'. Если второй UPDATE не доехал — блип
к БД или ноль строк потому, что поллер успел её подхватить, — крупная операция,
которая должна была ждать решения владельца, уходит в работу без него. Гейт
падал ОТКРЫТЫМ: самое опасное направление отказа из двух возможных.

Своих вызывающих у функции пока нет (так сказано в её докстринге), поэтому дыра
не выстрелила. Но она лежит готовой: следующий вызывающий унаследует её молча.

КАК ПОЧИНЕНО. Отказ переведён в закрытое направление: не сумев поставить гейт,
функция ОТМЕНЯЕТ операцию через дверь шины и поднимает исключение. Владелец
получит отказ вместо несогласованной рассылки, а операция, которую он не
одобрял, не уйдёт в работу ни при какой ошибке записи.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    """Пул, у которого второй шаг (постановка гейта) не проходит."""

    def __init__(self, mode: str):
        self.mode = mode
        self.updates = 0

    async def execute(self, sql, *a):
        if "waiting_approval" in sql:
            self.updates += 1
            if self.mode == "raises":
                raise RuntimeError("db down")
            return "UPDATE 0"          # поллер успел подхватить
        return "UPDATE 1"

    async def fetchrow(self, sql, *a):
        return None

    async def fetch(self, sql, *a):
        return []


@pytest.fixture
def _stubs(monkeypatch):
    from services import operation_bus

    state = {"cancelled": []}

    async def _submit(pool, owner_id, op_type, params, total_items=0, **kw):
        return 4242

    async def _cancel(pool, op_id, owner_id, allow=None):
        state["cancelled"].append((op_id, owner_id, tuple(allow or ())))
        return True

    monkeypatch.setattr(operation_bus, "submit", _submit)
    monkeypatch.setattr(operation_bus, "cancel", _cancel)
    return state


@pytest.mark.parametrize("mode", ["raises", "zero_rows"])
def test_a_gate_that_did_not_stand_cancels_the_operation(_stubs, mode):
    from database import db

    pool = _Pool(mode)
    with pytest.raises(Exception):
        asyncio.run(db.enqueue_op_with_approval(
            pool, owner_id=7, op_type="mass_invite", params={},
            total_items=5000, threshold=100))

    assert _stubs["cancelled"], (
        "гейт не встал, а операция осталась в очереди 'pending' — крупная "
        "операция уйдёт в работу без подтверждения владельца")
    op_id, owner_id, allow = _stubs["cancelled"][0]
    assert op_id == 4242 and owner_id == 7
    assert "running" in allow, (
        "в `allow` нет 'running': ноль строк на гейте значит, что поллер уже "
        "подхватил операцию — именно её и нужно успеть отменить")


def test_a_small_operation_needs_no_gate_and_no_cancel(_stubs):
    """Ниже порога гейт не ставится, и отменять, разумеется, нечего."""
    from database import db

    pool = _Pool("raises")
    op_id = asyncio.run(db.enqueue_op_with_approval(
        pool, owner_id=7, op_type="mass_invite", params={},
        total_items=10, threshold=100))
    assert op_id == 4242
    assert pool.updates == 0, "гейт поставлен операции ниже порога"
    assert not _stubs["cancelled"], "отменена операция, которой гейт не нужен"
