"""Регрессия: непрочитанный суточный бюджет больше не выдаёт флот как свежий.

РАЗРЫВ. Поверх лимитов отдельных операций есть общий суточный бюджет
риск-действий на аккаунт: без него один аккаунт за день участвует во множестве
операций — сверхчеловеческая активность и сигнатура фермы.

Счёт действий за сутки читался мягко: сбой БД давал пустой счёт, а пустой
читается как «сделано 0». Фильтр пропускал ВСЕХ, остаток считался полным — то
есть бюджет переставал существовать ровно тогда, когда база подрагивает, и
операции шли выжатыми аккаунтами.

Теперь отказ: аккаунты с непрочитанным счётом считаются исчерпавшими бюджет.
Дверь выбора не отдаёт ни одного, исполнитель откладывает операцию как при
занятом флоте и доработает следующим кругом. Отложенная операция дешевле бана.
"""
from __future__ import annotations

import asyncio

import pytest

from services import account_budget as ab


class _Pool:
    def __init__(self, fail: int, rows: list[dict] | None = None):
        self.fail = fail
        self.rows = rows or []
        self.reads = 0

    async def fetch(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась на счёте действий")
        return self.rows

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "OK"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    async def _fast(_s):
        return None
    monkeypatch.setattr(ab.asyncio, "sleep", _fast)


@pytest.fixture
def counted(monkeypatch):
    seen: list[tuple] = []
    from services import metrics
    monkeypatch.setattr(metrics, "inc",
                        lambda name, labels=None: seen.append((name, labels)))
    return seen


def test_the_count_refuses_instead_of_reporting_zero(counted):
    """Не прочитали счёт — отказ, а не «сделано 0»."""
    pool = _Pool(fail=2)
    with pytest.raises(ab.BudgetUnreadable):
        _run(ab.actions_today_bulk(pool, [1, 2]))
    assert pool.reads == 2, f"блип обязан лечиться повтором: чтений {pool.reads}"
    assert ("infragram_daily_limit_read_failures_total", {"where": "budget"}) in counted, (
        f"сбой чтения бюджета не виден на графике: {counted}")


def test_unknown_means_exhausted_for_the_filter():
    """Фильтр не выдаёт ни одного аккаунта, пока счёт неизвестен."""
    within, over = _run(ab.filter_within_budget(_Pool(fail=2), [1, 2, 3], limit=50))
    assert within == [], (
        "фильтр выдал аккаунты с неизвестным счётом — именно так бюджет и "
        "переставал действовать")
    assert over == [1, 2, 3], "исчерпавшими должны считаться все три"


def test_unknown_means_no_remainder():
    """Остаток на прогон — ноль, а не полный бюджет заново."""
    assert _run(ab.remaining_bulk(_Pool(fail=2), [7], limit=50)) == {7: 0}


def test_the_healthy_path_is_unchanged():
    """Счёт читается — фильтр и остаток считают как раньше."""
    rows = [{"account_id": 1, "n": 60}, {"account_id": 2, "n": 5}]
    within, over = _run(ab.filter_within_budget(_Pool(fail=0, rows=rows), [1, 2], limit=50))
    assert (within, over) == ([2], [1])
    assert _run(ab.remaining_bulk(_Pool(fail=0, rows=rows), [1, 2], limit=50)) == {1: 0, 2: 45}


def test_the_executor_postpones_instead_of_working(monkeypatch):
    """Реальный путь исполнителя: аккаунтов не осталось, значит операция подождёт.

    Исполнитель уже умеет пустой список — он откладывает операцию так же, как
    при занятом флоте. Важно именно это: не «работаем всеми», а «подождём».
    """
    from services import op_worker

    accounts = [{"id": 1}, {"id": 2}]
    kept, skipped = _run(op_worker._filter_over_budget_accounts(
        _Pool(fail=2), 555, accounts))
    assert kept == [], (
        "исполнитель пошёл работать аккаунтами с неизвестным суточным счётом")
    assert skipped == len(accounts), f"пропущенные не посчитаны: {skipped}"
