"""Сбой чтения суточного счёта выключал лимит аккаунта молча.

РАЗРЫВ. Суточный лимит действий аккаунта — защита от бана: он считается ПО
ЖУРНАЛУ (`operation_audit` за 24 часа), поэтому переживает рестарт и деплой.
Но читается он запросом, а сбой запроса трактовался как «сделано 0»: фильтр
пропускал всех, остаток считался полным, и аккаунт получал дневную норму
заново — второй раз за те же сутки.

Решение остаться fail-open сознательное и записано в `remaining_bulk`: сбой
учёта не должен останавливать операцию. Молчание — не часть этого решения.
Раньше о сбое говорил только `log.debug`, то есть никто: защита переставала
работать, и узнать об этом было негде.

ЧТО ПРОВЕРЯЕМ.
1. Одна повторная попытка: сетевой блип до базы больше не выключает лимит
   вовсе — счёт перечитывается.
2. Если не прочитался и со второй попытки, поведение прежнее (fail-open), но
   сбой виден: `log.error` и счётчик, который сторож защитных записей доносит
   до владельца словами.
3. Все известные места, где читается «сделано за сутки», этот счётчик
   увеличивают — иначе следующая такая защита снова сломается в тишине.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "services", "op_worker.py")
COUNTER = "infragram_daily_limit_read_failures_total"


class _Pool:
    """Падает на первых `fail_times` запросах, дальше отвечает честно."""

    def __init__(self, fail_times: int, n: int = 7):
        self.fail_times = fail_times
        self.calls = 0
        self._n = n

    async def fetch(self, sql, *a):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("соединение с базой потеряно")
        return [{"account_id": 1, "n": self._n}]


@pytest.fixture
def budget(monkeypatch):
    from services import account_budget, metrics

    metrics.reset()

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(account_budget.asyncio, "sleep", _no_sleep)
    return account_budget


def _counter_total(name: str = COUNTER) -> float:
    from services import metrics

    total = 0.0
    for line in metrics.render().splitlines():
        if line.startswith(name):
            total += float(line.rsplit(" ", 1)[-1])
    return total


# ── Повтор: блип больше не выключает лимит ──────────────────────────────────

def test_a_blip_is_retried_and_the_limit_still_works(budget):
    pool = _Pool(fail_times=1, n=7)

    counts = asyncio.run(budget.actions_today_bulk(pool, [1]))

    assert counts == {1: 7}, (
        "счёт не перечитан — одна неудачная попытка выключала суточный лимит")
    assert _counter_total() == 0, "блип, который мы пережили, не сбой защиты"


def test_a_real_failure_is_loud(budget):
    pool = _Pool(fail_times=5)

    counts = asyncio.run(budget.actions_today_bulk(pool, [1, 2]))

    assert counts == {1: 0, 2: 0}, (
        "поведение при сбое менять не планировали: fail-open записан в коде "
        "осознанно (сбой учёта не останавливает операцию)")
    assert _counter_total() >= 1, (
        "сбой чтения снова молчит — защита перестала работать, и узнать об "
        "этом негде")


def test_the_filter_still_lets_work_happen_when_the_count_is_unreadable(budget):
    """Fail-open проверяем прямо, чтобы следующий читатель видел: это решение."""
    pool = _Pool(fail_times=5)

    within, over = asyncio.run(budget.filter_within_budget(pool, [1, 2], limit=10))

    assert within == [1, 2] and over == []


# ── Сбой доходит до человека ────────────────────────────────────────────────

def test_the_failure_reaches_the_owner(monkeypatch):
    """Счётчик живёт в памяти процесса; владельцу его доносит сторож."""
    from services import metrics, op_worker

    metrics.reset()
    metrics.inc(COUNTER, {"where": "budget"})

    sent: list[tuple[int, str]] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((int(chat_id), str(text)))

    class _P:
        async def execute(self, *a, **k):
            return "UPDATE 1"

        async def fetchrow(self, *a, **k):
            return None

        async def fetch(self, *a, **k):
            return []

    async def _true():
        return True

    monkeypatch.setattr(op_worker.db, "notify_dedup_ok",
                        lambda *a, **k: _true(), raising=False)
    import bot.utils.subscription as subs
    monkeypatch.setattr(subs, "_admin_ids", lambda: {777}, raising=False)

    asyncio.run(op_worker._watchdog_protective_failures(_P(), _Bot(), {}))

    assert sent, "сбой чтения суточного лимита не дошёл ни до кого"
    assert "суточный лимит" in sent[0][1], sent[0][1]


# ── Закрытая перепись мест чтения ───────────────────────────────────────────

READ_SITES = {
    "joins_today = await pool.fetchval(": "bulk_join",
    "leaves_today = await pool.fetchval(": "bulk_leave",
    "created_today = await _safe_fetchval(": "bulk_create_channels",
}


def test_every_place_that_reads_todays_count_shouts_on_failure():
    """Иначе лимит снова выключится в тишине — уже в другом исполнителе."""
    with open(WORKER, encoding="utf-8") as fh:
        src = fh.read()
    silent = []
    for anchor, where in sorted(READ_SITES.items()):
        at = src.find(anchor)
        assert at > 0, f"{where}: чтение счёта за сутки пропало — поправьте ориентир"
        if COUNTER not in src[at:at + 1800]:
            silent.append(where)
    assert not silent, (
        "эти места читают «сделано за сутки» и при сбое молча считают, что "
        f"сделано ноль: {silent}. Аккаунт получит дневную норму второй раз, "
        "и узнать об этом будет негде — увеличьте " + COUNTER)


def test_the_counter_is_described_for_the_owner():
    """Счётчик без слов владельцу — это снова лог, который он не читает."""
    from services import metrics, op_worker

    assert COUNTER in metrics._HELP, "счётчик не описан в реестре метрик"
    words = op_worker._PROTECTIVE_FAILURE_COUNTERS.get(COUNTER, "")
    assert "лимит" in words and len(words) > 40, (
        "в сообщении владельцу нет слов про суточный лимит: " + repr(words))


def test_the_census_anchors_are_not_a_single_place():
    """Самопроверка: перепись смотрит в три разных места, а не в одно."""
    with open(WORKER, encoding="utf-8") as fh:
        src = fh.read()
    found = sorted(src.find(a) for a in READ_SITES)
    assert len(set(found)) == 3 and all(x > 0 for x in found)
    assert max(found) - min(found) > 1000, "ориентиры сошлись в одну точку"
