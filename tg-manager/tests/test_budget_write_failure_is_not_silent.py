"""Сбой записи суточного бюджета аккаунта не должен быть молчаливым.

РАЗРЫВ. Суточный бюджет действий — защита от спам-фильтров Telegram: аккаунт,
совершивший слишком много действий за сутки, попадает под них. Считается он по
ПОСТРОЧНЫМ записям в `operation_audit`, которые делает
`account_budget.record_actions` ПОСЛЕ работы: исполнитель отдал n сделанных
действий, модуль их записал.

Запись шла одним `pool.execute`, а её сбой — в `log.debug` и всё. То есть при
недоступном Postgres (деплой, рестарт базы, занятый пул) действия аккаунта
просто не попадали в счёт. Последствие прямое: `filter_within_budget` видит у
аккаунта меньше действий, чем он сделал, и следующая операция спокойно берёт
его сверх суточного лимита. Это ровно тот риск бана, от которого бюджет и
придуман, — и узнать о нём было негде: ни ошибки, ни счётчика, ни слова
владельцу.

Тот же класс в этом коде уже разобран у соседей — дедуп инвайта, строка успеха
в журнале, пауза аккаунта: у каждой защитной записи есть повторная попытка,
log.error и счётчик, а рост счётчика сторож воркера доносит до владельца
словами. Запись бюджета была последней защитной записью без этого.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def budget():
    from services import account_budget
    return account_budget


class _Pool:
    """Пул, который падает заданное число первых попыток."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0

    async def execute(self, q, *a):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("БД недоступна")
        return "INSERT 0 1"


@pytest.fixture
def counters(monkeypatch):
    from services import metrics

    seen: list[tuple] = []

    def _inc(name, labels=None, value=1.0):
        seen.append((name, labels, value))

    monkeypatch.setattr(metrics, "inc", _inc)
    return seen


def _record(budget, pool, **kw):
    return asyncio.run(budget.record_actions(
        pool, 777, 42, kw.pop("action", "join"), kw.pop("n", 3), **kw))


# ── Повторная попытка и громкий отказ ────────────────────────────────────────

def test_a_transient_failure_is_retried(budget, counters):
    """Типовая причина мгновенная (занятый пул, блокировка) — проходит сама."""
    pool = _Pool(fail_times=1)
    _record(budget, pool)
    assert pool.calls == 2, (
        f"повторной попытки записи бюджета нет (попыток {pool.calls})")
    assert not [c for c in counters if "budget" in c[0]], (
        "удавшийся повтор не должен считаться потерей")


def test_a_lost_write_is_counted_and_loud(budget, counters):
    """Молчание здесь стоит бана: следующая операция возьмёт аккаунт сверх лимита."""
    pool = _Pool(fail_times=99)
    _record(budget, pool)
    names = [c[0] for c in counters]
    assert "infragram_budget_write_failures_total" in names, (
        f"потеря записи бюджета не попала ни в один счётчик: {names}")


def test_it_never_raises(budget, counters):
    """Зовётся после работы: исключение здесь обнулило бы отчёт исполнителя."""
    _record(budget, _Pool(fail_times=99))


def test_a_healthy_write_is_one_statement(budget, counters):
    """Самопроверка: иначе тесты выше ничего не измеряют."""
    pool = _Pool()
    _record(budget, pool)
    assert pool.calls == 1
    assert not [c for c in counters if "budget" in c[0]]


def test_nothing_is_written_for_an_uncounted_action(budget, counters):
    """Пассивные действия в бюджет не входят — и повторять их нечего."""
    pool = _Pool(fail_times=99)
    _record(budget, pool, action="health_check")
    assert pool.calls == 0 and not counters


# ── Связка: рост счётчика доходит до человека ───────────────────────────────

def test_the_counter_reaches_the_owner_in_words():
    """Счётчик живёт в памяти процесса и наружу отдаётся только на /metrics,
    поэтому сторож защитных записей обязан знать про него и называть его
    словами владельца."""
    from services import op_worker

    table = op_worker._PROTECTIVE_FAILURE_COUNTERS
    assert "infragram_budget_write_failures_total" in table, (
        "сторож защитных записей не знает про потерю записи бюджета — "
        "владелец о ней не узнает")
    phrase = table["infragram_budget_write_failures_total"]
    assert phrase and any("а" <= ch.lower() <= "я" for ch in phrase), (
        f"объяснение владельцу не по-русски: {phrase!r}")


def test_the_counter_is_documented_in_the_registry():
    """Храповик реестра метрик иначе покраснит ветку уже после пуша."""
    from services.metrics import _HELP

    assert "infragram_budget_write_failures_total" in _HELP, (
        "счётчик не описан в реестре метрик")
    assert any("а" <= ch.lower() <= "я"
               for ch in _HELP["infragram_budget_write_failures_total"])
