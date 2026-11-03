"""Сторож цикла событий (services/loop_lag.py) видит замирание и того, кто держит.

Блокирующий код в корутине останавливает весь процесс — бот, мини-апп,
операции — и при этом не пишет в лог ни строчки. Сторож обязан: (1) измерить
задержку в метрики, (2) снять стек потока цикла, пока тот стоит, и назвать
функцию, которая его держит, (3) не снимать стек повторно на то же замирание.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from services import loop_lag, metrics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _blocking_culprit(sec: float) -> None:
    end = time.perf_counter() + sec
    while time.perf_counter() < end:
        pass


async def test_stall_is_measured_and_culprit_named(caplog):
    metrics.reset()
    caplog.set_level(logging.WARNING, logger="services.loop_lag")

    async def scenario():
        task = asyncio.create_task(
            loop_lag.run(interval=0.05, stall_after=0.2, dump_after=0.3,
                         dump_every=60.0))
        await asyncio.sleep(0.2)            # пульс пошёл
        _blocking_culprit(0.8)              # цикл встал
        await asyncio.sleep(0.2)            # пульс замерил опоздание
        snap = loop_lag.snapshot()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return snap

    snap = await scenario()

    assert snap["running"] is True
    assert snap["max_lag_sec_1m"] >= 0.5
    gauges = metrics.snapshot()["gauges"]
    assert gauges[loop_lag.LAG_MAX] >= 0.5
    assert metrics.snapshot()["counters"][loop_lag.STALLS] >= 1

    dumps = [r.getMessage() for r in caplog.records if "стоит уже" in r.getMessage()]
    assert len(dumps) == 1, dumps                    # одно замирание — один снимок
    assert "_blocking_culprit" in dumps[0]           # назван виновник
    assert loop_lag.snapshot()["running"] is False   # отмена снимает флаг


def test_watchdog_dumps_once_per_stall_and_respects_rate():
    dog = loop_lag._Watchdog(stall_after=1.0, dump_every=60.0, poll=0.1)
    now = 1000.0
    with loop_lag._lock:
        loop_lag._state.update(last_beat=now, loop_thread_id=None)
    assert dog.check_once(now + 0.5) is None          # ещё не замирание
    assert dog.check_once(now + 1.5) is not None      # снимок
    assert dog.check_once(now + 3.0) is None          # то же замирание — молчим
    with loop_lag._lock:
        loop_lag._state["last_beat"] = now + 10       # пульс был, новое замирание
    assert dog.check_once(now + 12) is None           # но реже раза в минуту — нет
    with loop_lag._lock:
        loop_lag._state["last_beat"] = now + 100
    assert dog.check_once(now + 102) is not None


def test_metrics_are_described_and_watchdog_started_in_every_role():
    for name in (loop_lag.LAG, loop_lag.LAG_MAX, loop_lag.STALLS):
        assert name in metrics._HELP, name
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        src = fh.read()
    # _web_resilient, а не _resilient: под ROLE=web бот и мини-апп тоже
    # страдают от замирания цикла, а _resilient в этой роли не запускает ничего.
    assert '_web_resilient("loop_lag", _loop_lag.run)' in src
