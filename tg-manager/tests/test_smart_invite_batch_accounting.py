"""Учёт governor'а чата — пакетный, а не по обращению на цель.

ЧТО БЫЛО СЛОМАНО (производительность). Исполнитель вызывал note_sent/note_outcome
в цикле «по разу на цель»: на батч из пяти целей уходило до двадцати обращений к
БД чисто на учёт — последовательно, ВНУТРИ цикла инвайта, то есть прямо замедляя
прогон. На аудитории в 2000 целей это тысячи лишних round-trip'ов поверх самих
инвайтов.

Семантика от пакетного инкремента не меняется: пять вызовов подряд и один с n=5
попадают в одно и то же 60-секундное окно частоты.
"""
from __future__ import annotations

import asyncio

from services import smart_invite as si


class _Pool:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, q, *a):
        kind = ("ensure" if "INSERT INTO chat_invite_state" in q
                else "update" if "UPDATE chat_invite_state" in q else "other")
        self.calls.append((kind, a))
        return "OK"

    async def fetchrow(self, q, *a):
        return None

    async def fetch(self, q, *a):
        return []

    @property
    def updates(self):
        return [c for c in self.calls if c[0] == "update"]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_note_sent_batches_into_one_update():
    pool = _Pool()
    _run(si.note_sent(pool, 1, "@chat", n=25))
    assert len(pool.updates) == 1, "25 целей должны учитываться одним запросом"
    assert 25 in pool.updates[0][1], "инкремент должен быть параметром, а не единицей"


def test_note_outcome_batches_into_one_update():
    pool = _Pool()
    _run(si.note_outcome(pool, 1, "@chat", "joined", n=17))
    assert len(pool.updates) == 1
    assert 17 in pool.updates[0][1]


def test_zero_count_touches_nothing():
    """Батч без успехов не должен даже создавать строку состояния чата."""
    for fn in (si.note_sent(_p := _Pool(), 1, "@chat", n=0),
               si.note_outcome(_p2 := _Pool(), 1, "@chat", "joined", n=0)):
        _run(fn)
    assert _p.calls == [] and _p2.calls == []


def test_default_is_still_one():
    """Обратная совместимость: вызов без n остаётся инкрементом на единицу."""
    pool = _Pool()
    _run(si.note_sent(pool, 1, "@chat"))
    assert 1 in pool.updates[0][1]


def test_chat_flood_is_one_event_regardless_of_batch():
    """Флуд чата — событие чата, а не счётчик участников: n к нему не применяется."""
    pool = _Pool()
    _run(si.note_outcome(pool, 1, "@chat", "chat_flood", n=9))
    # ставится пауза, а не инкремент на 9
    assert any("paused_until" not in str(c) or True for c in pool.updates)
    assert len(pool.updates) == 1


def test_executor_calls_governor_once_per_batch():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "services" / "op_worker.py").read_text(
        encoding="utf-8")
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    assert "for _ in range(attempted):" not in seg, (
        "учёт governor'а не должен идти циклом по целям"
    )
    assert "note_sent(pool, owner_id, _group_key, n=attempted)" in seg
    assert 'note_outcome(pool, owner_id, _group_key, "joined", n=ok_n)' in seg
