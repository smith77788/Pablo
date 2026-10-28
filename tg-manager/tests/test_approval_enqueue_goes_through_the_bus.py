"""Постановка операции с подтверждением идёт через шину, а не мимо неё.

ЧТО БЫЛО. `database/db.py` умел ставить операцию прямым `INSERT INTO
operation_queue` — постановка «с ожиданием подтверждения владельца». Прямая
запись обходит разом всё, что шина делает по дороге:

  * гейт тарифа: free-юзер поставил бы платную операцию;
  * предохранитель Ban Weather: операция встала бы и тогда, когда этот тип
    прямо сейчас массово убивает аккаунты владельца;
  * окно идемпотентности: двойной тап кнопки дал бы ДВЕ операции, то есть
    двойной расход аккаунтов и удвоенный риск;
  * проверку, что op_type вообще существует в реестре — иначе операция висит
    pending вечно, потому что исполнять её некому.

Вызывающих у функции не было, то есть дыра ещё не выстрелила. Но она лежала
готовой к употреблению, а храповик `test_no_raw_queue_inserts` смотрел только в
`bot/` и `services/` и в `database/` не заглядывал.

ЧТО ТЕПЕРЬ. Операция ставится через `operation_bus.submit`, ожидание
подтверждения проставляется вторым шагом поверх уже поставленной строки. Зона
храповика расширена на слой базы.
"""
from __future__ import annotations

import pytest


class _Pool:
    def __init__(self):
        self.updates: list[tuple] = []

    async def execute(self, query, *args):
        self.updates.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []


@pytest.mark.asyncio
async def test_operation_is_submitted_through_the_bus(monkeypatch):
    from database import db
    from services import operation_bus

    seen: dict = {}

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        seen["args"] = (owner_id, op_type, params, kw)
        return 77

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)

    op_id = await db.enqueue_op_with_approval(
        _Pool(), 5, "mass_publish", {"text": "пост"}, total_items=3)

    assert op_id == 77
    assert seen["args"][1] == "mass_publish", (
        "операция поставлена мимо шины — обойдены гейт тарифа, предохранитель "
        "и дедуп двойного тапа"
    )
    assert seen["args"][3].get("total_items") == 3


@pytest.mark.asyncio
async def test_a_small_operation_is_not_held_for_approval(monkeypatch):
    from database import db
    from services import operation_bus

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        return 77

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    pool = _Pool()

    await db.enqueue_op_with_approval(pool, 5, "mass_publish", {}, total_items=3, threshold=20)

    assert not any("waiting_approval" in q for q, _ in pool.updates)


@pytest.mark.asyncio
async def test_a_large_operation_waits_for_approval(monkeypatch):
    from database import db
    from services import operation_bus

    async def _fake_submit(pool, owner_id, op_type, params, **kw):
        return 77

    monkeypatch.setattr(operation_bus, "submit", _fake_submit)
    pool = _Pool()

    await db.enqueue_op_with_approval(pool, 5, "mass_publish", {}, total_items=50, threshold=20)

    held = [q for q, _ in pool.updates if "waiting_approval" in q]
    assert held, "операция сверх порога не ждёт подтверждения владельца"
    assert "status='pending'" in held[0], (
        "статус меняется без проверки: операция, уже взятая воркером, была бы "
        "отброшена обратно в ожидание прямо во время исполнения"
    )


def test_the_ratchet_watches_the_database_layer():
    """Слепая зона храповика закрыта — иначе дыра вернётся незамеченной."""
    import tests.test_no_raw_queue_inserts as ratchet

    assert "database" in ratchet._LAYERS
    assert any(rel.startswith("database/") for rel, _ in ratchet._sources())
