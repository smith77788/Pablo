"""Операция, остановленная по бюджету живучести, не исчезает молча.

ЧТО БЫЛО. Две защиты гасят операцию, исчерпавшую бюджет воскрешений: сброс на
старте воркера (`_reset_stale_running`) и сторож зависших (`_watchdog_stale`).
Оба делали это одним общим UPDATE — атомарно и правильно, — и на этом
останавливались. Операция оставалась недозакрытой:

  * статус. Ровный `failed` при любом объёме сделанного. А бюджет живучести
    тратят именно ДЛИННЫЕ операции: Railway шлёт SIGTERM на каждом деплое,
    поэтому многочасовая рассылка под три деплоя подряд успевает взять
    большую часть целей и всё равно объявляется ядовитой;
  * `result`. Закрытие с result=NULL — ни бот, ни мини-апп не покажут ни одной
    цифры, только текст «остановлена»;
  * `infragram_operations_total`, событие `op_done`, `compliance_engine.record`.
    Собственный счётчик `infragram_op_poisoned_total` у этого исхода был, но на
    общем графике исходов, в памяти организма и в подписанном аудит-трейле его
    не было вовсе;
  * слово владельцу. Про флуд-паузу, занятый флот и возврат в очередь ему
    пишут. Здесь операция просто исчезала из «выполняется» — худший случай для
    молчания, потому что сама она больше не стартует никогда.

Чужой терминальный статус при этом не переписывается: дозакрытие трогает строку
только при `status='failed'`, то есть ровно ту, которую сторож погасил сам.
Отмена владельца ('cancelled') под это условие не попадает.
"""
from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from services import op_status, op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _fn(src: str, name: str) -> str:
    start = src.index(f"async def {name}(")
    m = re.search(r"\nasync def |\ndef ", src[start + 10:])
    return src[start:start + 10 + m.start()] if m else src[start:]


class _Pool:
    def __init__(self, *, ok_n=0, failed_n=0):
        self.ok_n, self.failed_n = ok_n, failed_n
        self.writes: list[tuple] = []

    async def fetchrow(self, query, *args):
        # Персистентный анти-повтор уведомлений: первая попытка всегда
        # проходит (в проде INSERT .. RETURNING отдаёт строку).
        if "notification_dedup" in query:
            return {"user_id": 555}
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.writes.append((query, args))
        return "UPDATE 1"

    def status_write(self):
        for query, args in self.writes:
            if "SET status=$2" in query:
                return query, args
        return None, None


class _Bot:
    pass


def _row(**over):
    row = {"id": 42, "owner_id": 555, "op_type": "mass_publish",
           "params": json.dumps({"account_ids": [7]}),
           "done_items": 0, "total_items": 0}
    row.update(over)
    return row


@pytest.fixture
def spies(monkeypatch):
    seen: dict = {"metric": [], "compliance": [], "spine": [], "notified": []}

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

    async def _notify(pool, bot, owner_id, kind, text, **kw):
        seen["notified"].append((owner_id, text))
    monkeypatch.setattr(op_worker.db, "notify_if_enabled", _notify)
    return seen


def _finish(pool, row=None, source="watchdog"):
    async def _go():
        await op_worker._finish_poisoned_op(
            pool, row if row is not None else _row(),
            source=source, bot=_Bot())
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# ── Сделанное не исчезает ────────────────────────────────────────────────────

def test_work_already_done_makes_it_partial(spies):
    """Рассылка под три деплоя взяла большую часть целей — это не «ничего»."""
    pool = _Pool(ok_n=840, failed_n=12)
    _finish(pool, _row(done_items=840, total_items=1000))
    _query, args = pool.status_write()
    assert op_status.PARTIAL in args, (
        "операция, взявшая 840 целей, закрыта ровным провалом"
    )


def test_nothing_done_stays_a_failure(spies):
    pool = _Pool(ok_n=0)
    _finish(pool)
    _query, args = pool.status_write()
    assert op_status.FAILED in args


def test_result_carries_the_counters(spies):
    pool = _Pool(ok_n=840, failed_n=12)
    _finish(pool, _row(done_items=840, total_items=1000))
    _query, args = pool.status_write()
    payload = next((json.loads(a) for a in args
                    if isinstance(a, str) and a.startswith("{")), None)
    assert payload, "операция закрыта с result=NULL — показать нечего"
    assert (payload["ok"], payload["failed"]) == (840, 12)
    assert payload["status"] == op_status.PARTIAL


def test_the_write_cannot_overwrite_a_cancel(spies):
    pool = _Pool(ok_n=5)
    _finish(pool)
    query, _args = pool.status_write()
    assert "status='failed'" in query, (
        "дозакрытие обязано трогать только строку, которую сторож погасил сам"
    )
    assert "acct_wait_since=NULL" in query


# ── Исход виден снаружи ──────────────────────────────────────────────────────

def test_outcome_reaches_the_general_graph(spies):
    pool = _Pool(ok_n=5)
    _finish(pool)
    names = [n for n, _ in spies["metric"]]
    assert "infragram_operations_total" in names, (
        "у исхода был только свой счётчик — на общем графике его не было вовсе"
    )


def test_outcome_is_signed_in_the_audit_trail(spies):
    pool = _Pool(ok_n=5)
    _finish(pool)
    assert spies["compliance"] == [("mass_publish", op_status.PARTIAL, 42)]


def test_organism_learns_about_the_outcome(spies):
    pool = _Pool(ok_n=5)
    _finish(pool)
    assert "op_done" in [k for k, _ in spies["spine"]]


def test_owner_is_told_the_operation_stopped(spies):
    """Сама она больше не стартует никогда — молчание здесь дороже всего."""
    pool = _Pool(ok_n=840)
    _finish(pool, _row(done_items=840, total_items=1000))
    assert spies["notified"], "операция исчезла из «выполняется» без единого слова"
    _owner, text = spies["notified"][0]
    assert "840" in text
    assert "заново" in text, "владельцу не сказали, что делать дальше"


def test_owner_hears_the_plain_truth_when_nothing_ran(spies):
    pool = _Pool(ok_n=0)
    _finish(pool)
    _owner, text = spies["notified"][0]
    assert "ни одной цели" in text
    assert "прокси" in text, "подсказка о причине полезнее, чем сухой отказ"


# ── Оба сторожа дозакрывают, а не только один ────────────────────────────────

@pytest.mark.parametrize("guard", ["_reset_stale_running", "_watchdog_stale"])
def test_both_guards_finish_what_they_poison(guard):
    body = _fn(_read("services/op_worker.py"), guard)
    assert "RETURNING id, owner_id, op_type, params" in body, (
        f"{guard} гасит операции, не зная какие — дозакрыть их нечем"
    )
    assert "_finish_poisoned_op(" in body, (
        f"{guard} оставляет операцию недозакрытой: без result, подписи и слова "
        f"владельцу"
    )


@pytest.mark.parametrize("guard", ["_reset_stale_running", "_watchdog_stale"])
def test_both_guards_can_reach_the_owner(guard):
    """Без bot уведомление молча не уходит — путь обязан его получать."""
    src = _read("services/op_worker.py")
    assert f"async def {guard}(pool: asyncpg.Pool, bot=None)" in src, (
        f"{guard} не получает bot — слово владельцу некуда отправить"
    )
    run = _fn(src, "run")
    assert f"{guard}(pool, bot)" in run, f"в {guard} из цикла воркера bot не передан"


# ── Путь не падает, когда читать нечего ──────────────────────────────────────

def test_a_row_without_id_is_ignored(spies):
    pool = _Pool(ok_n=5)
    _finish(pool, {"owner_id": 555})
    assert pool.status_write() == (None, None)
    assert not spies["notified"]


def test_unparsable_params_do_not_stop_the_close(spies):
    pool = _Pool(ok_n=5)
    _finish(pool, _row(params="не json"))
    _query, args = pool.status_write()
    assert op_status.PARTIAL in args
    assert spies["compliance"], "исход не подписан из-за нечитаемых params"
