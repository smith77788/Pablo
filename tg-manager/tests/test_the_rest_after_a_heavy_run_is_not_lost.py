"""Отдых аккаунта после тяжёлого прогона не теряется молча.

РАЗРЫВ. `apply_post_action_cooldown` — это персональный rate-limit аккаунта:
удар делает 100+ действий записи, поэтому тот же аккаунт обязан отдохнуть
часы, прежде чем его возьмут во второй такой прогон. Запись этого отдыха в
`tg_accounts` уходила в `log.warning` и наружу — в `None`: вызывающий (а это
`strike_engine`, оба его пути) не мог отличить поставленный отдых от
потерянного.

Почему памяти процесса недостаточно: `state.cooldown_until` живёт до
рестарта, а аккаунты следующей операции выбираются по базе. То есть
потерянная запись — это аккаунт, который после рестарта выглядит отдохнувшим
и уходит во второй удар сразу. Это не неполный отчёт, а выключенный
предохранитель.

ЧТО ПРОВЕРЯЕМ: одна повторная попытка на месте (типовая причина мгновенная),
при полном отказе — `False`, громкий `log.error` словами о последствии и
счётчик, который сторож защитных записей превращает в сообщение владельцу.
Отдых в памяти ставится в любом случае: пока процесс жив, аккаунт припаркован.
"""
from __future__ import annotations

import asyncio
import logging

import pytest


class _Pool:
    """Падает на первых `fail` попытках записи, дальше отвечает успехом."""

    def __init__(self, fail: int = 0):
        self.fail = int(fail)
        self.calls = 0

    async def execute(self, *a, **k):
        self.calls += 1
        if self.calls <= self.fail:
            raise RuntimeError("соединение с БД потеряно")
        return "UPDATE 1"


@pytest.fixture
def fe(monkeypatch):
    from services import flood_engine, metrics

    metrics.reset()
    # Повтор проверяем по числу попыток, а не по паузе: настоящий sleep
    # запоминаем ДО подмены, иначе заглушка позовёт саму себя.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(flood_engine.asyncio, "sleep",
                        lambda *_a, **_k: _real_sleep(0))
    return flood_engine


def _state(fe, acc: int):
    st = fe.get_account_state(acc)
    st.cooldown_until = 0.0
    return st


def test_a_blip_is_retried_and_the_rest_lands(fe):
    pool = _Pool(fail=1)
    _state(fe, 5001)
    ok = asyncio.run(fe.apply_post_action_cooldown(pool, 5001, 4 * 3600, "strike"))
    assert ok is True, "мгновенный сбой записи похоронил отдых аккаунта"
    assert pool.calls == 2, f"повторной попытки не было (попыток {pool.calls})"


def test_a_lost_rest_is_reported_and_not_claimed_as_done(fe, caplog):
    from services import metrics

    pool = _Pool(fail=2)
    _state(fe, 5002)
    with caplog.at_level(logging.ERROR):
        ok = asyncio.run(
            fe.apply_post_action_cooldown(pool, 5002, 4 * 3600, "strike"))

    assert ok is False, "потерянный отдых выдан вызывающему за поставленный"
    assert pool.calls == 2, "попыток должно быть ровно две"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ОТДЫХ НЕ ЗАПИСАН" in text, text
    assert "второй" in text, "в логе не названо последствие"

    counters = metrics.snapshot()["counters"]
    hit = [k for k in counters if k.startswith(
        "infragram_account_rest_write_failures_total")]
    assert hit, ("счётчик потерянного отдыха не вырос — сторож защитных записей "
                 "владельцу ничего не скажет")
    assert counters[hit[0]] == 1


def test_the_in_process_rest_is_set_even_when_the_write_fails(fe):
    st = _state(fe, 5003)
    asyncio.run(fe.apply_post_action_cooldown(_Pool(fail=2), 5003, 4 * 3600, "strike"))
    assert st.cooldown_until > 0, (
        "пока процесс жив, аккаунт обязан быть припаркован даже без записи в БД")


def test_an_existing_longer_rest_is_not_shortened(fe):
    st = _state(fe, 5004)
    st.cooldown_until = 10 ** 12  # уже стоит долгая пауза
    asyncio.run(fe.apply_post_action_cooldown(_Pool(), 5004, 60, "strike"))
    assert st.cooldown_until == 10 ** 12, "короткий отдых затёр долгую паузу"


def test_without_a_pool_nothing_is_claimed_as_failed(fe):
    _state(fe, 5005)
    assert asyncio.run(
        fe.apply_post_action_cooldown(None, 5005, 4 * 3600, "strike")) is True


def test_the_counter_is_declared_to_the_owner():
    """Иначе сбой защиты снова происходит молча: см. сторож защитных записей."""
    from services import metrics, op_worker

    name = "infragram_account_rest_write_failures_total"
    assert name in op_worker._PROTECTIVE_FAILURE_COUNTERS
    assert name in metrics._HELP
