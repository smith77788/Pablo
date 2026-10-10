"""Операция рассылки живёт столько, сколько идёт рассылка, и говорит правду.

ЧТО БЫЛО. `_exec_run_broadcast` делал три вещи: запускал фоновую задачу,
писал `done_items = total` и возвращал `done`. Операция рапортовала
«2000/2000, готово» в ту же секунду, когда не ушло ещё ни одного сообщения.

Следствий три, и все видел владелец:
  * счётчик врал, а `_progress_monitor` рассылал по нему бодрые отчёты;
  * отмена не работала: к моменту нажатия «Отменить» операция уже терминальна,
    а задача отправки живёт отдельно и про отмену не знает;
  * оборвавшаяся рассылка числилась успешной: операция в `done`, поэтому ни
    сторож зависших, ни повтор её не касались, и половина аудитории молча
    оставалась без сообщения.

Плюс два скрытых дубля в той же функции:
  * созданный `broadcast_id` не возвращался в params операции — любой повтор
    заводил ВТОРУЮ рассылку и слал всей аудитории заново (журнал доставок
    первой рассылки второй не указ, он ведётся по её собственному id);
  * при создании рассылки не сохранялся `target_user_ids` — прерванная
    СЕГМЕНТНАЯ рассылка догонялась после рестарта по всей аудитории бота.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker, broadcaster
from database import db as _db


class _FakePool:
    def __init__(self, broadcast_states, op_status_seq=None):
        self.broadcast_states = list(broadcast_states)
        self.op_status_seq = list(op_status_seq or [])
        self.executed: list[tuple[str, tuple]] = []
        self.done_items: list[int] = []

    async def fetchrow(self, query, *args):
        if "FROM managed_bots" in query:
            return {"token": "1:AA", "bot_id": 555}
        if "FROM broadcasts" in query:
            if len(self.broadcast_states) > 1:
                return self.broadcast_states.pop(0)
            return self.broadcast_states[0]
        if "SELECT status FROM operation_queue" in query:
            if self.op_status_seq:
                st = self.op_status_seq.pop(0) if len(self.op_status_seq) > 1 else self.op_status_seq[0]
                return {"status": st}
            return {"status": "running"}
        return None

    async def fetch(self, query, *args):
        if "FROM bot_users" in query:
            return [{"user_id": i} for i in range(1, 11)]
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "SET done_items=" in query:
            self.done_items.append(int(args[0]))
        return "UPDATE 1"

    def wrote(self, needle):
        return [a for q, a in self.executed if needle in q]


def _bc(status, sent, failed=0):
    return {"status": status, "sent_count": sent, "failed_count": failed}


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(op_worker, "_BROADCAST_POLL_S", 0, raising=False)
    op_worker._cancel_cache.clear()


@pytest.fixture
def _stub_broadcaster(monkeypatch):
    calls = {"start": [], "cancel": [], "running": True}
    monkeypatch.setattr(broadcaster, "start",
                        lambda *a, **kw: calls["start"].append((a, kw)))
    monkeypatch.setattr(broadcaster, "cancel",
                        lambda bid: calls["cancel"].append(bid) or True)
    monkeypatch.setattr(broadcaster, "is_running", lambda bid: calls["running"])
    return calls


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _exec(pool, params):
    return _run(op_worker._exec_run_broadcast(pool, None, 42, 777, dict(params)))


BASE = {"bot_id": 555, "broadcast_id": 900, "text": "привет"}


def test_progress_is_real_not_instant_total(_stub_broadcaster):
    pool = _FakePool([_bc("running", 0), _bc("running", 4), _bc("done", 10)])
    res = _exec(pool, BASE)
    assert pool.done_items[0] == 0, (
        "до первой отправки прогресс операции обязан быть нулевым, а не total"
    )
    assert pool.done_items[-1] == 10
    assert res["status"] == "done" and res["ok"] == 10


def test_partial_broadcast_reported_partial(_stub_broadcaster):
    pool = _FakePool([_bc("partial", 8, 2)])
    res = _exec(pool, BASE)
    assert res["status"] == "partial"
    # ok/failed читает classify_final: без ok рассылка с ошибками легла бы в failed
    assert res["ok"] == 8 and res["failed"] == 2


def test_cancel_stops_the_sending(_stub_broadcaster, monkeypatch):
    marked: list[tuple] = []

    async def _upd(pool, bid, sent, failed, status):
        marked.append((bid, status))

    monkeypatch.setattr(_db, "update_broadcast", _upd)
    pool = _FakePool([_bc("running", 3)], op_status_seq=["cancelled"])
    res = _exec(pool, BASE)
    assert _stub_broadcaster["cancel"] == [900], (
        "отмена операции обязана останавливать саму отправку, иначе сообщения "
        "продолжают уходить после нажатия «Отменить»"
    )
    assert marked == [(900, "cancelled")]
    assert res["status"] == "cancelled"


def test_already_running_broadcast_is_not_started_twice(_stub_broadcaster):
    pool = _FakePool([_bc("done", 10)])
    _exec(pool, BASE)
    assert _stub_broadcaster["start"] == [], (
        "вторая задача на тот же broadcast_id — двойная отправка по одной аудитории"
    )


def test_broadcast_id_is_pinned_into_op_params(_stub_broadcaster, monkeypatch):
    created = {}

    async def _create(pool, bot_id, text, total, owner, **kw):
        created.update(kw)
        return 901

    monkeypatch.setattr(_db, "create_broadcast", _create)
    _stub_broadcaster["running"] = False
    pool = _FakePool([_bc("done", 10)])
    params = {"bot_id": 555, "text": "привет"}
    _exec(pool, params)
    pinned = pool.wrote("SET params =")
    assert pinned, (
        "созданный broadcast_id не сохранён в params — повтор операции заведёт "
        "вторую рассылку и отправит всей аудитории заново"
    )
    assert "901" in str(pinned[0][0])


def test_segment_create_saves_target_list(_stub_broadcaster, monkeypatch):
    created = {}

    async def _create(pool, bot_id, text, total, owner, **kw):
        created.update(kw)
        return 902

    monkeypatch.setattr(_db, "create_broadcast", _create)
    _stub_broadcaster["running"] = False
    pool = _FakePool([_bc("done", 10)])
    _exec(pool, {"bot_id": 555, "text": "привет", "segment": "active_7d"})
    assert created.get("target_user_ids"), (
        "сегментная рассылка без target_user_ids после рестарта догоняется по "
        "ВСЕЙ аудитории бота"
    )
    assert len(created["target_user_ids"]) == 10


def test_full_audience_create_keeps_target_null(_stub_broadcaster, monkeypatch):
    created = {}

    async def _create(pool, bot_id, text, total, owner, **kw):
        created.update(kw)
        return 903

    monkeypatch.setattr(_db, "create_broadcast", _create)
    _stub_broadcaster["running"] = False
    pool = _FakePool([_bc("done", 10)])
    _exec(pool, {"bot_id": 555, "text": "привет"})
    assert created.get("target_user_ids") is None, (
        "полная аудитория обязана оставаться NULL: сохранённый список заморозил "
        "бы состав подписчиков на момент запуска"
    )


def test_orphaned_task_is_revived_once(_stub_broadcaster):
    """Задача рассылки исчезла, а статус не терминальный — поднимаем её."""
    _stub_broadcaster["running"] = False
    states = [_bc("running", 2)] * 5 + [_bc("done", 10)]
    pool = _FakePool(states)
    res = _exec(pool, BASE)
    # Первый start — обычный запуск (is_running=False), второй — подъём сироты.
    assert len(_stub_broadcaster["start"]) == 2, (
        "исчезнувшую задачу рассылки поднимаем ровно один раз"
    )
    assert res["status"] == "done"


def test_dead_broadcast_is_not_revived_forever(_stub_broadcaster):
    """Поднялась и снова умерла — честный partial, а не вечное ожидание.

    Операция, которая крутится в цикле подъёма, держала бы слот параллельности
    и никогда бы не завершилась: снаружи это тот же «вечный running», от
    которого бережётся вся остальная очередь.
    """
    _stub_broadcaster["running"] = False
    pool = _FakePool([_bc("running", 3)])
    res = _exec(pool, BASE)
    assert len(_stub_broadcaster["start"]) == 2
    assert res["status"] == "partial" and res["ok"] == 3
    assert "оборвалась" in res["summary"]
