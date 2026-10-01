"""Третий сторож зависших операций закрывает их так же полно, как воркер.

ЧТО БЫЛО. Сторожей осиротевших операций три, и только этот живёт в другом модуле
(services/account_monitor). Фильтр `status='running'` и слово владельцу у него
были, остального — нет:

  * статус. Ровный `failed` при любом объёме сделанного. А операция висит здесь
    часами именно потому, что успела поработать и оборвалась на ответе Telegram
    или прокси, — взятые цели у неё почти всегда есть;
  * `result`. Закрытие с result=NULL: ни бот, ни мини-апп не покажут ни одной
    цифры;
  * объявление исхода. Ни графика исходов, ни события шины, ни подписи в
    аудит-трейле — хотя тот обещает единый choke point и ВСЕ операции.

И текст владельцу нарушал язык продукта: «Статус изменён на failed»,
«Operations → Отчёты» — английские слова в сообщении, которое читает владелец,
не понимающий английского, — плюс сырой идентификатор типа операции («Тип:
mass_invite») вместо человеческого названия.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class _Pool:
    def __init__(self, *, ok_n=0, failed_n=0, rows_updated=1, hours=9):
        self.ok_n, self.failed_n = ok_n, failed_n
        self.rows_updated = rows_updated
        self.hours = hours
        self.updates: list = []

    async def fetch(self, query, *args):
        if "operation_queue" in query:
            return [{
                "id": 78, "owner_id": 5, "op_type": "mass_invite",
                "started_at": dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(hours=self.hours),
            }]
        return []

    async def fetchrow(self, query, *args):
        if "operation_log" in query:
            return {"ok_n": self.ok_n, "failed_n": self.failed_n}
        return None

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        self.updates.append((query, args))
        return f"UPDATE {self.rows_updated}"

    def close_write(self):
        for query, args in self.updates:
            if "SET status=$3" in query:
                return query, args
        return None, None


@pytest.fixture
def spies(monkeypatch):
    seen: dict = {"metric": [], "compliance": [], "spine": [], "notified": []}

    from services import account_monitor, compliance_engine as _ce, metrics as _m
    from services import op_worker
    from services.organism import spine as _spine

    async def _active():
        return set()
    monkeypatch.setattr(op_worker, "active_op_ids", _active)

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
    monkeypatch.setattr(account_monitor.db, "notify_if_enabled", _notify)
    return seen


async def _sweep(pool):
    from services import account_monitor

    await account_monitor._recover_stuck_operations(pool, None)


# ── Сделанное не исчезает ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_work_already_done_makes_it_partial(spies):
    pool = _Pool(ok_n=510, failed_n=22)
    await _sweep(pool)
    _query, args = pool.close_write()
    assert args, "осиротевшая операция не закрыта вовсе"
    assert "partial" in args, (
        "операция, успевшая взять 510 целей, объявлена полным провалом"
    )


@pytest.mark.asyncio
async def test_nothing_done_stays_a_failure(spies):
    pool = _Pool(ok_n=0)
    await _sweep(pool)
    _query, args = pool.close_write()
    assert "failed" in args


@pytest.mark.asyncio
async def test_result_carries_the_counters(spies):
    pool = _Pool(ok_n=510, failed_n=22)
    await _sweep(pool)
    _query, args = pool.close_write()
    payload = next((json.loads(a) for a in args
                    if isinstance(a, str) and a.startswith("{")), None)
    assert payload, "операция закрыта с result=NULL — показать нечего"
    assert (payload["ok"], payload["failed"]) == (510, 22)


@pytest.mark.asyncio
async def test_the_outcome_is_announced(spies):
    pool = _Pool(ok_n=510)
    await _sweep(pool)
    assert "infragram_operations_total" in [n for n, _ in spies["metric"]]
    assert "op_done" in [k for k, _ in spies["spine"]]
    assert spies["compliance"] == [("mass_invite", "partial", 78)]


@pytest.mark.asyncio
async def test_a_cancelled_operation_is_not_reported(spies):
    """Ноль строк = владелец уже закрыл операцию сам, пока она висела."""
    pool = _Pool(ok_n=5, rows_updated=0)
    await _sweep(pool)
    assert not spies["notified"] and not spies["compliance"]


# ── Текст владельцу ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_message_has_no_english_words(spies):
    """Владелец НЕ понимает английский — в его сообщениях его быть не может."""
    pool = _Pool(ok_n=510)
    await _sweep(pool)
    assert spies["notified"]
    _owner, text = spies["notified"][0]
    for bad in ("failed", "Operations", "running", "partial"):
        assert bad not in text, f"в сообщении владельцу английское слово: {bad}"


@pytest.mark.asyncio
async def test_the_message_names_the_operation_not_its_identifier(spies):
    pool = _Pool(ok_n=510)
    await _sweep(pool)
    _owner, text = spies["notified"][0]
    assert "mass_invite" not in text, (
        "владельцу показан идентификатор типа операции вместо названия"
    )
    assert "510" in text, "не сказано, сколько целей успело отработать"


@pytest.mark.asyncio
async def test_the_message_says_what_to_do_next(spies):
    pool = _Pool(ok_n=0)
    await _sweep(pool)
    _owner, text = spies["notified"][0]
    assert "заново" in text and "Отчёты" in text


# ── Фильтр статуса на месте ──────────────────────────────────────────────────

def test_the_close_still_spares_a_finished_operation():
    body = _read("services/account_monitor.py")
    seg = body[body.index("async def _close_orphan_op"):]
    seg = seg[:seg.index("\nasync def ", 10)]
    assert "status='running'" in seg, (
        "закрытие затрёт итог операции, которую исполнитель уже записал"
    )
    assert "_journal_counters(" in seg, "счётчики снова берутся не из журнала"
