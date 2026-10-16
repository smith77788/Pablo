"""Отменённая операция рассказывает, что успела сделать.

Отмена — терминальный статус наравне с done/partial/failed, но закрывалась она
двумя короткими UPDATE и `return`, минуя весь остальной финал. Из-за этого
отменённая операция теряла:

  * `result` — владелец видел «Отменено» без единой цифры, хотя часть целей до
    стопа уже отработана. Для инвайта и рассылки «сколько успело уйти» —
    главный вопрос после отмены, по нему решают, продолжать ли;
  * сброс `acct_wait_since` — повтор той же операции наследовал накопленное
    ожидание флота, по которому `_requeue_op_no_accounts` проваливает операцию
    за «ждёт слишком долго»;
  * метрику `infragram_operations_total` — отмен не было видно на графике, то
    есть «владелец всё отменяет» выглядело как тишина;
  * `compliance_engine.record` — аудит-трейл обещает единый choke point и ВСЕ
    операции, а отменённые в него не попадали;
  * событие шины `op_done` — память организма об отмене не узнавала.

Статус при этом не воскрешается: запись трогает строку только если она уже
'cancelled' (отменил владелец) либо ещё не терминальная (отменил исполнитель).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from services import op_worker, op_status

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _run_op_task_body() -> str:
    ow = _read("services/op_worker.py")
    start = ow.index("async def _run_op_task(")
    return ow[start:ow.index("async def _exec_bulk_bot_edit")]


class _FakePool:
    def __init__(self):
        self.writes: list[tuple] = []

    async def execute(self, query, *args):
        self.writes.append((query, args))
        return "UPDATE 1"

    async def fetchrow(self, query, *args):
        return None

    async def fetch(self, query, *args):
        return []


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def spies(monkeypatch):
    """Перехватываем все четыре канала итога, чтобы видеть, куда дошла отмена."""
    seen: dict = {"metric": [], "compliance": [], "spine": []}

    from services import metrics as _m, compliance_engine as _ce
    from services.organism import spine as _spine

    monkeypatch.setattr(_m, "inc", lambda name, labels=None: seen["metric"].append(
        (name, dict(labels or {}))))
    monkeypatch.setattr(_m, "observe", lambda *a, **k: None)

    async def _record(pool, owner_id, acc_id, op_type, status, **kw):
        seen["compliance"].append((op_type, status, kw.get("op_id")))
    monkeypatch.setattr(_ce, "record", _record)

    async def _emit(pool, owner_id, kind, payload):
        seen["spine"].append((kind, payload))
    monkeypatch.setattr(_spine, "emit", _emit)
    return seen


def _finish(pool, result, spies=None):
    return _run(op_worker._finish_cancelled_op(
        pool, 42, 777, "mass_invite", {"account_ids": [1]}, result, 12.5))


def _status_write(pool) -> tuple[str, tuple]:
    for q, args in pool.writes:
        if "UPDATE operation_queue" in q and "status='cancelled'" in q:
            return q, args
    raise AssertionError(f"записи статуса отмены нет вовсе: {pool.writes}")


# ── Итог доходит до владельца ────────────────────────────────────────────────

def test_counters_of_the_work_done_before_the_stop_are_saved(spies):
    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 7, "failed": 2,
                   "summary": "Отменено. Приглашено: 7"})

    q, args = _status_write(pool)
    saved = json.loads(args[0])
    assert saved["ok"] == 7 and saved["failed"] == 2, (
        "без счётчиков владелец не знает, сколько целей уже израсходовано")
    assert saved["status"] == op_status.CANCELLED
    assert "7" in saved["summary"]


def test_fleet_wait_mark_is_cleared_for_the_retry(spies):
    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 1, "failed": 0})

    q, _ = _status_write(pool)
    assert "acct_wait_since=NULL" in q, (
        "повтор унаследует старое ожидание флота и провалится за «ждёт долго»")


def test_cancellation_is_visible_on_the_graph(spies):
    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 1, "failed": 0})

    assert ("infragram_operations_total",
            {"op_type": "mass_invite", "status": op_status.CANCELLED}) in spies["metric"]


def test_cancellation_reaches_the_audit_trail(spies):
    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 1, "failed": 0})

    assert spies["compliance"] == [("mass_invite", op_status.CANCELLED, 42)], (
        "аудит-трейл обещает ВСЕ операции единым choke point")


def test_cancellation_reaches_the_organism(spies):
    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 3, "failed": 0})

    kinds = [k for k, _ in spies["spine"]]
    assert "op_done" in kinds, "память организма не узнаёт об отмене"


# ── Чего делать нельзя ───────────────────────────────────────────────────────

def test_a_finished_operation_is_never_resurrected():
    """Гонка реальна: пока исполнитель сворачивался, операция могла закрыться."""
    pool = _FakePool()
    _finish(pool, {"status": "cancelled"})

    ow = _read("services/op_worker.py")
    helper = ow[ow.index("async def _finish_cancelled_op"):][:3000]
    assert "sql_terminal_list" in helper, (
        "нет защиты от перезаписи терминального статуса")
    q, _ = _status_write(pool)
    assert "status='cancelled' OR status NOT IN" in q, (
        "запись обязана щадить уже закрытую операцию и при этом доходить до "
        "строки, которую владелец уже перевёл в 'cancelled'")


def test_the_first_finish_time_is_kept():
    """Отменил владелец — время отмены ближе к правде, чем конец сворачивания."""
    pool = _FakePool()
    _finish(pool, {"status": "cancelled"})

    q, _ = _status_write(pool)
    assert "finished_at=COALESCE(finished_at, now())" in q


def test_side_channels_cannot_break_the_status_write(monkeypatch):
    """Метрика, аудит и шина — вспомогательные: их сбой не роняет закрытие."""
    from services import metrics as _m, compliance_engine as _ce
    from services.organism import spine as _spine

    def _boom(*a, **k):
        raise RuntimeError("упало")

    async def _aboom(*a, **k):
        raise RuntimeError("упало")

    monkeypatch.setattr(_m, "inc", _boom)
    monkeypatch.setattr(_ce, "record", _aboom)
    monkeypatch.setattr(_spine, "emit", _aboom)

    pool = _FakePool()
    _finish(pool, {"status": "cancelled", "ok": 5, "failed": 0})
    assert json.loads(_status_write(pool)[1][0])["ok"] == 5


# ── Связка с прогоном ────────────────────────────────────────────────────────

def test_both_ways_of_cancelling_go_through_the_same_finish():
    body = _run_op_task_body()
    assert body.count("_finish_cancelled_op(") == 1, (
        "один путь закрытия отмены, иначе половина итога снова потеряется")
    assert 'result.get("status") == "cancelled"' in body, (
        "отмену видит сам исполнитель — этот случай обязан дойти до финала")
    assert re.search(r'current\["status"\]\s*==\s*"cancelled"', body), (
        "отмену нажал владелец — этот случай тоже")


def test_the_old_short_circuit_is_gone():
    """Именно эти два UPDATE и теряли итог отмены."""
    body = _run_op_task_body()
    assert "UPDATE operation_queue SET status='cancelled', finished_at=now() " not in body
    assert "AND finished_at IS NULL" not in body
