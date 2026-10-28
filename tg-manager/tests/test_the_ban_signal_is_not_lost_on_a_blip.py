"""Сигнал бана не теряется от одного блипа к базе, и потеря не молчит.

`record_account_restriction` — единственный вход сигнала об ограничении
аккаунта. Эту запись читают четыре предохранителя: гейт операций (аккаунт под
ограничением в работу не берём), риск-пульс на экране флота, `anti_storm`
(волна банов → глубокий сон флота) и мониторинг.

Потерянная запись здесь — не неполный отчёт, а выключенный предохранитель:
аккаунт не уйдёт в карантин и будет взят в следующую же операцию, а волна банов
его не посчитает. Ровно тот разрыв, ради закрытия которого функция и появилась
(«предохранитель против волны банов не мог сработать на бане никогда»), только
для одного аккаунта и из-за блипа.

Запись идёт из хука бана, а не по каждой цели, поэтому повтор ничего не стоит.
"""
from __future__ import annotations

import asyncio
import logging


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """Пул, который падает на первых `fail` записях."""

    def __init__(self, fail: int):
        self.fail = fail
        self.writes = 0

    async def execute(self, query, *args):
        self.writes += 1
        if self.writes <= self.fail:
            raise ConnectionResetError("БД отвалилась")
        return "INSERT 0 1"


def test_a_single_blip_does_not_lose_the_signal():
    """Один сбой — не причина оставить забаненный аккаунт без карантина."""
    from services import infra_memory

    pool = _Pool(fail=1)
    ok = _run(infra_memory.record_account_restriction(
        pool, owner_id=77, account_id=5, event_type="banned"))

    assert ok is True, (
        "сигнал бана потерян после одного блипа: аккаунт не уйдёт в карантин "
        "и будет взят в следующую операцию")
    assert pool.writes == 2, f"повтора не было: записей {pool.writes}"


def test_a_lost_signal_says_so_out_loud(caplog):
    """Не записали и со второго раза — запись уровня error с последствием."""
    from services import infra_memory

    pool = _Pool(fail=99)
    with caplog.at_level(logging.ERROR, logger="services.infra_memory"):
        ok = _run(infra_memory.record_account_restriction(
            pool, owner_id=77, account_id=5, event_type="banned"))

    assert ok is False, "вызывающего не роняем: операция уже наткнулась на бан"
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "потеря сигнала бана не оставила записи уровня error — отключившийся "
        "предохранитель не отличить от предохранителя, которому нечего сказать")


def test_a_lost_signal_raises_the_counter_the_owner_hears(monkeypatch):
    """Счётчик потери — единственный способ, которым владелец об этом узнаёт.

    Логи он не читает; рост этого счётчика сторож воркера переводит в слова
    (op_worker._PROTECTIVE_FAILURE_COUNTERS).
    """
    from services import infra_memory, metrics

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        metrics, "inc", lambda name, labels=None: seen.append((name, labels or {})))

    _run(infra_memory.record_account_restriction(
        _Pool(fail=99), owner_id=77, account_id=5, event_type="banned"))

    assert any(n == "infragram_restriction_write_failures_total" for n, _l in seen), (
        f"счётчик потери сигнала не вырос: {seen} — владелец о потере не узнает")


def test_the_healthy_path_writes_once():
    """Здоровый путь: одна запись, один ответ."""
    from services import infra_memory

    pool = _Pool(fail=0)
    assert _run(infra_memory.record_account_restriction(
        pool, owner_id=77, account_id=5, event_type="banned")) is True
    assert pool.writes == 1


def test_an_incomplete_call_is_not_a_write():
    """Без владельца/аккаунта/типа писать нечего — и в базу не ходим."""
    from services import infra_memory

    pool = _Pool(fail=99)
    assert _run(infra_memory.record_account_restriction(
        pool, owner_id=0, account_id=5, event_type="banned")) is False
    assert pool.writes == 0
