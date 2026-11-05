"""Переданный подарок не передаётся второй раз после рестарта.

Передача подарка необратима и стоит звёзд, а повтор прогона — штатное событие:
рестарт воркера возвращает операцию в очередь и обнуляет `done_items`, после
чего исполнитель заново читает план. Дверь здесь одна: в работу берутся только
подарки в статусах 'pending'/'queued', а уже переданные из списка выпадают.
Дверь эта — одна строка условия, и её легко потерять при любой правке отбора,
поэтому на неё стоит храповик.

ЧТО ЭТИМ НЕ ПРОВЕРЯЕТСЯ. Отметка 'transferred' пишется ПОСЛЕ самой передачи, и
в зазоре между ними остаётся щель: процесс умер — подарок ушёл, а в базе он
'pending'. Второй передачи это не даёт (подарок адресуется msg_id сохранённого
подарка и после передачи у отправителя его уже нет, повтор получит ошибку), но
итог в отчёте окажется «не удалось» у подарка, который получатель получил.
Разбор и причина, по которой это не правится здесь, — в CLAUDE.md.
"""
from __future__ import annotations

import pytest


class _Pool:
    """Минимальная подделка: подарки в инвентаре передаваемы, записи копим."""

    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, query, *args):
        self.executed.append(" ".join(str(query).split()))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        if "gift_inventory" in query:
            return {"is_transferable": True}
        return None

    async def fetchval(self, query, *args):
        return 0


def _item(item_id: int, status: str) -> dict:
    return {
        "id": item_id, "inventory_id": 500 + item_id, "account_id": 7,
        "gift_id": str(1000 + item_id), "gift_type": "star", "stars_cost": 25,
        "status": status, "retry_count": 0, "max_retries": 3,
    }


@pytest.fixture
def plan_with_one_done(monkeypatch):
    """План: один подарок уже передан, один ждёт. Телеграм заменён счётчиком."""
    from services import gift_operation as go
    from services.gift_transfer import GiftTransferService
    from services.gift_report import GiftTransferReportService
    from services import op_worker

    plan = {
        "id": 42, "recipient_user_id": 999, "recipient_username": "",
        "items": [_item(1, "transferred"), _item(2, "pending")],
    }
    attempted: list[int] = []
    statuses: list[tuple[int, str]] = []

    async def _fake_transfer(pool, item, uid, uname):
        attempted.append(int(item["id"]))
        return go.GiftTransferResult(item_id=item["id"], success=True, cost=25)

    async def _fake_status(pool, item_id, status, **kw):
        statuses.append((int(item_id), status))

    monkeypatch.setattr(go, "_transfer_single_gift", _fake_transfer)
    monkeypatch.setattr(GiftTransferService, "get_plan",
                        staticmethod(lambda pool, pid: _async(plan)))
    monkeypatch.setattr(GiftTransferService, "mark_plan_running",
                        staticmethod(lambda pool, pid: _async(None)))
    monkeypatch.setattr(GiftTransferService, "mark_plan_done",
                        staticmethod(lambda pool, pid, cost: _async(None)))
    monkeypatch.setattr(GiftTransferService, "update_item_status",
                        staticmethod(_fake_status))
    monkeypatch.setattr(GiftTransferReportService, "generate_report",
                        staticmethod(lambda pool, pid, oid: _async(7)))
    monkeypatch.setattr(op_worker, "_declare_total_items",
                        lambda pool, op_id, total: _async(None))
    monkeypatch.setattr(op_worker, "_is_cancelled", lambda pool, op_id: _async(False))
    return plan, attempted, statuses


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_the_resumed_run_skips_the_gift_already_given(plan_with_one_done):
    from services import gift_operation as go

    _plan, attempted, _statuses = plan_with_one_done
    res = await go._exec_gift_transfer(_Pool(), 77, {"plan_id": 42})

    assert attempted == [2], (
        "повторный прогон тронул уже переданный подарок — это вторая передача "
        f"необратимого подарка и вторая трата звёзд (пробовал: {attempted})")
    assert res["status"] == "done"


@pytest.mark.asyncio
async def test_the_outcome_counts_the_whole_plan_not_this_pass(plan_with_one_done):
    """Итог считается по плану, иначе возобновлённый прогон занижает сделанное."""
    from services import gift_operation as go

    res = await go._exec_gift_transfer(_Pool(), 77, {"plan_id": 42})

    assert res["transferred"] == 2, (
        "итог посчитан только по этому заходу: владельцу покажут «1 / 2», когда "
        f"переданы оба подарка ({res})")
    assert res["total"] == 2


def test_the_door_filters_by_status():
    """Отбор идёт по статусу подарка — условие на месте, а не «все подряд»."""
    import inspect

    from services import gift_operation as go

    src = inspect.getsource(go._exec_gift_transfer)
    assert 'item["status"] in ("pending", "queued")' in src, (
        "отбор подарков в работу больше не фильтрует по статусу: возобновлённый "
        "прогон попробует передать уже переданные")
