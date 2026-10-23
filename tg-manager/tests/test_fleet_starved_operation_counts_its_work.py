"""Операция, не дождавшаяся свободного флота, не притворяется незапущенной.

ЧТО БЫЛО. Это последний путь завершения, который закрывался одним UPDATE и
`return`, минуя весь остальной финал. Он писал ровный `failed`, текст «не
запустилась: все аккаунты были заняты» — и больше ничего.

Попасть сюда можно УЖЕ ПОСЛЕ РАБОТЫ. Рассылка берёт 300 целей, Telegram
назначает часовую паузу, `_defer_op_for_flood` возвращает операцию в очередь (её
собственный путь, он `acct_wait_since` не ставит) — и на возобновлении флот
занят другими операциями. Начинается отсчёт ожидания, через
`_ACCT_WAIT_MAX_MIN` операция проваливается, и владелец читает, что она не
запускалась. На инвайте — самой баноопасной операции продукта — это прямо ведёт
к повторному запуску вручную.

Потери были те же, что у отмены и у падения:

  * статус. `failed` вместо `partial` при взятых целях;
  * `result`. Закрытие с result=NULL: ни бот, ни мини-апп не могли показать ни
    одной цифры — ровно тогда, когда «сколько успело уйти» и есть вопрос;
  * метрика. Голодание по флоту не попадало ни на `infragram_operations_total`,
    ни на собственный счётчик: снаружи операции просто «не доезжали»;
  * `compliance_engine.record` и событие `op_done`. Аудит-трейл обещает единый
    choke point и ВСЕ операции, память организма учится на исходах — этот исход
    не видел ни один из них.

Счётчики берутся из журнала целей, а не из `done_items`: журнал переживает
возвраты в очередь, а `done_items` на каждом из них обнуляется.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os

import pytest

from services import op_status, op_worker

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _waited_too_long() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        minutes=op_worker._ACCT_WAIT_MAX_MIN + 5)


class _Pool:
    """Пул, различающий три запроса пути: отметка ожидания, карточка, журнал."""

    def __init__(self, *, ok_n=0, failed_n=0, op_type="mass_invite",
                 params=None, total_items=None, rows_updated=1,
                 waiting_since=None):
        self.ok_n, self.failed_n = ok_n, failed_n
        self.op_type = op_type
        self.params = params if params is not None else {"account_ids": [11]}
        self.total_items = total_items
        self.rows_updated = rows_updated
        self.waiting_since = waiting_since or _waited_too_long()
        self.writes: list[tuple] = []

    async def fetchrow(self, query, *args):
        # Персистентный анти-повтор уведомлений: первая попытка всегда
        # проходит (в проде INSERT .. RETURNING отдаёт строку).
        if "notification_dedup" in query:
            return {"user_id": 555}
        if "acct_wait_since FROM operation_queue" in query:
            return {"acct_wait_since": self.waiting_since}
        if "op_type, params" in query:
            return {"op_type": self.op_type,
                    "params": json.dumps(self.params),
                    "done_items": self.ok_n,
                    "total_items": self.total_items}
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        return None

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.writes.append((query, args))
        return f"UPDATE {self.rows_updated}"

    def final(self):
        for query, args in self.writes:
            if "finished_at=now()" in query:
                return query, args
        return None, None


class _Bot:
    pass


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


def _starve(pool, spies=None):
    async def _go():
        await op_worker._requeue_op_no_accounts(
            pool, 77, bot=_Bot(), owner_id=555)
    asyncio.get_event_loop_policy()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_go())
    finally:
        loop.close()


# ── Взятые цели не исчезают ──────────────────────────────────────────────────

def test_work_already_done_makes_it_partial_not_failed(spies):
    """300 отработанных целей — это `partial`, а не «ничего не вышло»."""
    pool = _Pool(ok_n=300)
    _starve(pool)
    query, args = pool.final()
    assert query, "операция не закрыта вовсе — она осталась бы в 'running'"
    assert op_status.PARTIAL in args, (
        "операция, успевшая взять 300 целей, закрыта ровным провалом"
    )


def test_nothing_done_is_still_an_honest_failure(spies):
    """Обратная сторона: без единой цели это настоящий провал."""
    pool = _Pool(ok_n=0)
    _starve(pool)
    _query, args = pool.final()
    assert op_status.FAILED in args


def test_result_carries_the_counters(spies):
    """Без `result` ни бот, ни мини-апп не покажут ни одной цифры."""
    pool = _Pool(ok_n=300, failed_n=7)
    _starve(pool)
    _query, args = pool.final()
    payload = next((json.loads(a) for a in args
                    if isinstance(a, str) and a.startswith("{")), None)
    assert payload, "операция закрыта с result=NULL"
    assert payload["ok"] == 300 and payload["failed"] == 7
    assert payload["status"] == op_status.PARTIAL


def test_counters_come_from_the_journal_not_done_items(spies):
    """done_items обнуляется каждым возвратом в очередь — журнал нет."""
    body = op_worker._finish_fleet_starved_op.__doc__ or ""
    assert "журнал" in body.lower()
    src = _read("services/op_worker.py")
    seg = src[src.index("async def _finish_fleet_starved_op"):]
    seg = seg[:seg.index("\nasync def ", 10)]
    assert "_journal_counters(" in seg, (
        "итог считается по done_items — он теряется на каждом возврате в очередь"
    )


# ── Исход виден снаружи ──────────────────────────────────────────────────────

def test_outcome_reaches_the_metrics(spies):
    pool = _Pool(ok_n=5)
    _starve(pool, spies)
    names = [n for n, _ in spies["metric"]]
    assert "infragram_operations_total" in names, (
        "исход не попал на общий график — снаружи операция просто «не доехала»"
    )
    assert "infragram_op_fleet_starved_total" in names, (
        "у голодания по флоту нет собственного счётчика: нельзя отличить "
        "«мало аккаунтов» от любых других провалов"
    )
    labels = dict(spies["metric"])["infragram_operations_total"]
    assert labels.get("status") == op_status.PARTIAL


def test_outcome_is_signed_in_the_audit_trail(spies):
    pool = _Pool(ok_n=5, op_type="mass_invite")
    _starve(pool, spies)
    assert spies["compliance"], (
        "аудит-трейл обещает единый choke point и ВСЕ операции"
    )
    op_type, status, op_id = spies["compliance"][0]
    assert (op_type, status, op_id) == ("mass_invite", op_status.PARTIAL, 77)


def test_organism_learns_about_the_outcome(spies):
    pool = _Pool(ok_n=5)
    _starve(pool, spies)
    kinds = [k for k, _ in spies["spine"]]
    assert "op_done" in kinds
    payload = dict(spies["spine"])["op_done"]
    assert payload["status"] == op_status.PARTIAL and payload["ok"] == 5


# ── Что читает владелец ──────────────────────────────────────────────────────

def test_owner_is_not_told_it_never_started(spies):
    """Текст «не запустилась» на 300 отправленных приглашениях — ложь."""
    pool = _Pool(ok_n=300)
    _starve(pool, spies)
    assert spies["notified"], "операция умерла молча"
    _owner, text = spies["notified"][0]
    assert "не запустилась" not in text, (
        "владельцу сказали, что операция не запускалась, хотя 300 целей ушло — "
        "на инвайте это прямо ведёт к повторному запуску вручную"
    )
    assert "300" in text


def test_owner_hears_the_plain_truth_when_nothing_ran(spies):
    pool = _Pool(ok_n=0)
    _starve(pool, spies)
    _owner, text = spies["notified"][0]
    assert "не запустилась" in text and "занят" in text


# ── Чужой терминальный статус не затирается ──────────────────────────────────

def test_cancel_by_the_owner_is_not_overwritten(spies):
    """Ноль обновлённых строк = операцию уже закрыли (почти всегда отменили)."""
    pool = _Pool(ok_n=5, rows_updated=0)
    _starve(pool, spies)
    assert not spies["notified"], (
        "владелец получил «провал» на то, что сам только что остановил"
    )
    assert not spies["compliance"], "в аудит ушёл исход, которого не было"
    assert not spies["metric"], "на график ушёл исход, которого не было"


def test_terminal_guard_is_in_the_query(spies):
    pool = _Pool(ok_n=5)
    _starve(pool, spies)
    query, _args = pool.final()
    assert "status NOT IN" in query, "провал затрёт отмену владельца"
    assert "acct_wait_since=NULL" in query, (
        "накопленное ожидание флота остаётся на закрытой операции"
    )


# ── Путь не падает, когда читать нечего ──────────────────────────────────────

def test_unreadable_card_still_closes_the_operation(spies):
    """Исключение здесь оставило бы операцию в 'running' до сторожа."""
    class _Blind(_Pool):
        async def fetchrow(self, query, *args):
            if "acct_wait_since FROM operation_queue" in query:
                return {"acct_wait_since": self.waiting_since}
            return None

    pool = _Blind()
    _starve(pool, spies)
    query, args = pool.final()
    assert query, "операция не закрыта: без карточки путь упал"
    assert op_status.FAILED in args
