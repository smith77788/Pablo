"""Регрессия: блип при закрытии больше не называет доведённую работу частичной.

РАЗРЫВ. `done_items` в очереди обнуляется на КАЖДОЙ новой попытке, а
`total_items` остаётся от первого замаха. Поэтому операция на 380 целей,
которая взяла 203, ушла в повтор после флуд-паузы и добрала остальные 177,
закрывалась с «177 из 380». Чтобы этого не было, итог считается по журналу:
сколько РАЗНЫХ целей операция закрыла за все свои прогоны
(`_own_journal_coverage`).

Но читался этот итог одним выстрелом: сбой запроса давал ноль, и защита
отключалась — доведённая работа снова объявлялась частичной, владелец получал
предложение повторить и эскалацию о том, что уже сделано.

Теперь у чтения есть повтор. Запасной ответ не меняется: «частично» на
неизвестности безопаснее «выполнено» (повтор идемпотентен, а тихо недоделанная
рассылка — нет). Повтор лишь делает эту неизвестность редкой.
"""
from __future__ import annotations

import asyncio

import pytest

from services import op_worker
from services import op_status


class _Pool:
    def __init__(self, fail: int, row=None):
        self.fail = fail
        self.row = row
        self.reads = 0

    async def fetchrow(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась на чтении итога")
        return self.row

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "OK"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _fast(_s):
        return None
    monkeypatch.setattr(op_worker.asyncio, "sleep", _fast)


def test_a_blip_no_longer_hides_the_coverage():
    """Один сбой — покрытие всё равно прочитано, итог честный."""
    pool = _Pool(fail=1, row={"n": 380})
    assert _run(op_worker._own_journal_coverage(pool, 555)) == 380
    assert pool.reads == 2, f"повтора не было: чтений {pool.reads}"


def test_a_blip_no_longer_hides_the_counters():
    """То же для счётчиков успехов и провалов на путях закрытия."""
    # fail=3: первые два чтения тратит цепочка повторов — она читается
    # терпимо (отчёт, не дверь идемпотентности) и тоже с повтором, после чего
    # отступает к журналу самой операции. Блип самого счёта — третье чтение,
    # его лечит повтор, и четвёртое отдаёт цифры.
    pool = _Pool(fail=3, row={"ok_n": 203, "failed_n": 0})
    assert _run(op_worker._journal_counters(pool, 555)) == (203, 0)
    assert pool.reads == 4


def test_the_fallback_is_still_the_safe_one():
    """Не прочитали и со второй попытки — прежний запасной ответ."""
    assert _run(op_worker._own_journal_coverage(_Pool(fail=5), 555)) == 0
    assert _run(op_worker._journal_counters(_Pool(fail=5), 555)) == (0, 0)


def test_why_it_matters_the_classifier_would_say_partial():
    """Смысл цифры: без покрытия доведённая операция называется частичной.

    Проверка на самом классификаторе, а не на пересказе: именно он превращает
    «177 из 380» в 'partial', и именно это владелец видит как недоделанную
    работу с предложением повторить.
    """
    assert op_status.classify_final("done", ok=177, failed=0,
                                    done_items=177, total_items=380) == op_status.PARTIAL
    assert op_status.classify_final("done", ok=177, failed=0,
                                    done_items=380, total_items=380) == op_status.DONE
