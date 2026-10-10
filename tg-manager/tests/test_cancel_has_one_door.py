"""Отмена операции — одна дверь, как `submit()` для постановки.

ЧТО БЫЛО. Двери не было: отмена жила пятью сырыми UPDATE по разным
поверхностям — мини-апп, два экрана бота, экран апрува, массовые операции, — и
каждая расходилась с остальными:

  * три из пяти не ставили `finished_at` вовсе: у отменённой операции не было
    времени завершения, то есть «когда закончилась» и «сколько шла» не мог
    ответить никто;
  * одна не фильтровала статус совсем и могла переписать итог уже завершённой
    операции (статус читался выше по коду — между чтением и записью операция
    успевает стартовать);
  * ни одна не дописывала `result` и не объявляла исход, поэтому отмена из
    очереди не попадала ни на график исходов, ни в подписанный аудит-трейл, ни
    в память организма. А `submit()` рядом эмитит `op_queued` и прямо обещает
    этим полный жизненный цикл операции.

Запущенную операцию закрывает `op_worker._finish_cancelled_op` (он увидит
'cancelled' и допишет итог сам), поэтому дверь дописывает итог только тем, кого
воркер уже не подхватит никогда.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Поверхности, которые отменяют операции владельца.
SURFACES = (
    "services/mini_app_api.py",
    "bot/handlers/approval_flow.py",
    "bot/handlers/mass_ops.py",
    "bot/handlers/botmother_menu.py",
)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _Pool:
    def __init__(self, *, was="pending", ok_n=0, failed_n=0, found=True):
        self.was, self.found = was, found
        self.ok_n, self.failed_n = ok_n, failed_n
        self.executed: list[tuple] = []

    async def fetchrow(self, query, *args):
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        if not self.found:
            return None
        self.executed.append((query, args))
        return {"was_status": self.was, "op_type": "mass_invite",
                "params": json.dumps({"account_ids": [3]})}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    def result_write(self):
        for query, args in self.executed:
            if "result=$2::jsonb" in query:
                return query, args
        return None, None


@pytest.fixture
def spies(monkeypatch):
    seen: dict = {"metric": [], "compliance": [], "spine": []}
    from services import compliance_engine as _ce, metrics as _m
    from services.organism import spine as _spine

    def _inc(name, labels=None, value=1.0):
        seen["metric"].append((name, dict(labels or {})))
    monkeypatch.setattr(_m, "inc", _inc)
    monkeypatch.setattr(_m, "observe", lambda *a, **k: None)

    async def _record(pool, owner_id, acc_id, op_type, status, **kw):
        seen["compliance"].append((op_type, status, kw.get("op_id")))
    monkeypatch.setattr(_ce, "record", _record)

    async def _emit(pool, owner_id, kind, payload):
        seen["spine"].append((kind, payload))
    monkeypatch.setattr(_spine, "emit", _emit)
    return seen


def _cancel(pool, **kw):
    from services import operation_bus as obus

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(obus.cancel(pool, 77, 555, **kw))
    finally:
        loop.close()


# ── Дверь закрывает операцию до конца ────────────────────────────────────────

def test_a_pending_cancel_writes_its_result(spies):
    """Воркер её уже не подхватит — значит итог обязана дописать дверь."""
    pool = _Pool(was="pending", ok_n=120, failed_n=4)
    assert _cancel(pool) is True
    _query, args = pool.result_write()
    assert args, "операция отменена с result=NULL — показать нечего"
    payload = json.loads(args[1])
    assert (payload["ok"], payload["failed"]) == (120, 4)
    assert payload["status"] == "cancelled"
    assert "120" in payload["summary"], (
        "владелец не узнает, сколько успело уйти до отмены — а именно по этому "
        "числу решают, продолжать ли с нуля"
    )


def test_the_outcome_is_announced(spies):
    pool = _Pool(was="pending", ok_n=5)
    _cancel(pool)
    assert "infragram_operations_total" in [n for n, _ in spies["metric"]]
    assert "op_done" in [k for k, _ in spies["spine"]], (
        "пара к op_queued не замкнулась: память организма об отмене не узнаёт"
    )
    assert spies["compliance"] == [("mass_invite", "cancelled", 77)]


def test_a_running_cancel_is_left_to_the_worker(spies):
    """Иначе исход объявился бы дважды: здесь и в _finish_cancelled_op."""
    pool = _Pool(was="running", ok_n=5)
    assert _cancel(pool) is True
    assert pool.result_write() == (None, None)
    assert not spies["spine"], "исход отменённой операции объявлен дважды"


def test_a_missing_operation_is_not_cancelled(spies):
    pool = _Pool(found=False)
    assert _cancel(pool) is False
    assert not spies["spine"]


# ── Что всегда есть в запросе ────────────────────────────────────────────────

def test_the_door_always_sets_the_finish_time():
    src = _read("services/operation_bus.py")
    door = src[src.index("async def cancel("):]
    door = door[:door.index("\nasync def ", 10)]
    assert "finished_at = NOW()" in door, (
        "у отменённой операции не будет времени завершения — «когда "
        "закончилась» и «сколько шла» не ответит никто"
    )
    assert "owner_id = $2" in door, "отмена без проверки владельца"
    assert "status = ANY($3::text[])" in door, (
        "набор разрешённых состояний ушёл из запроса: между проверкой и записью "
        "операция успевает стартовать"
    )
    assert "FOR UPDATE" in door, (
        "старый статус читается без блокировки — ответ «успел ли воркер взять "
        "её» может устареть ровно в эту миллисекунду"
    )


# ── Храповик: мимо двери отмену не пишут ────────────────────────────────────

def test_no_surface_cancels_an_operation_by_hand():
    offenders = []
    for rel in SURFACES:
        src = _read(rel)
        for m in re.finditer(r"UPDATE operation_queue", src):
            seg = src[m.start():m.start() + 400]
            nxt = seg.find("UPDATE operation_queue", 1)
            if nxt > 0:
                seg = seg[:nxt]
            if re.search(r"status\s*=\s*'cancelled'", seg.split("WHERE")[0]):
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"  {rel}:{line}")
    assert not offenders, (
        "отмена операции пишется мимо operation_bus.cancel — поверхности снова "
        "разойдутся в том, какие состояния отменяются, ставится ли время "
        "завершения и объявляется ли исход:\n" + "\n".join(offenders)
    )


def test_the_detector_sees_the_surfaces_at_all():
    """Детектор, который ничего не находит, зелёный всегда."""
    for rel in SURFACES:
        src = _read(rel)
        assert "operation_bus" in src and "cancel(" in src, (
            f"{rel} больше не отменяет операции — список поверхностей устарел"
        )
