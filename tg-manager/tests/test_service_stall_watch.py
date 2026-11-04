"""Сторож зависших фоновых сервисов (service_supervisor.run_stall_watch).

Присмотр лечит падение сервиса, но повисший проход (ожидание без потолка)
снаружи неотличим от работающего: задача жива, исключения нет, лог молчит.
Сторож обязан: сообщить о сервисе, который стоит на ОДНОМ ожидании дольше
порога, — с местом в коде и один раз; и НЕ сообщать о простое (sleep, событие,
очередь, ожидание с потолком, gather вечных циклов) и о проходе, который
работает (каждый раз ждёт новое).
"""
from __future__ import annotations

import asyncio
import os

from services import metrics
from services import service_supervisor as ss

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def _hung_pass():
    while True:
        # «полуоткрытый сокет»: ответа не будет никогда
        await asyncio.get_running_loop().create_future()


async def _sleepy():
    while True:
        await asyncio.sleep(3600)


async def _event_waiter():
    await asyncio.Event().wait()


async def _queue_waiter():
    await asyncio.Queue().get()


async def _bounded_waiter():
    await asyncio.wait_for(asyncio.Event().wait(), 3600)


async def _gathered_loops():
    await asyncio.gather(_sleepy(), _sleepy())


async def _busy_pass():
    while True:
        await asyncio.sleep(0)       # каждый раз новое ожидание — проход идёт
        fut = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(fut.set_result, None)
        await fut


def _start(name, coro_fn):
    task = asyncio.create_task(ss.supervise(name, coro_fn))
    return task


async def test_hung_service_is_reported_once_and_idle_ones_are_not():
    metrics.reset()
    ss._RUNNING.clear()
    alerts: list[tuple[str, str]] = []

    async def _alert(key, text):
        alerts.append((key, text))

    tasks = [
        _start("hung", _hung_pass),
        _start("sleepy", _sleepy),
        _start("event", _event_waiter),
        _start("queue", _queue_waiter),
        _start("bounded", _bounded_waiter),
        _start("gathered", _gathered_loops),
        _start("busy", _busy_pass),
    ]
    try:
        await asyncio.sleep(0.05)
        assert set(ss._RUNNING) == {"hung", "sleepy", "event", "queue",
                                    "bounded", "gathered", "busy"}
        watch = ss.StallWatch(stall_after=100.0, alert=_alert)
        assert await watch.check_once(now=1000.0) == []
        await asyncio.sleep(0.01)
        assert await watch.check_once(now=1050.0) == []      # ещё не порог
        await asyncio.sleep(0.01)
        assert await watch.check_once(now=1200.0) == ["hung"]
        await asyncio.sleep(0.01)
        assert await watch.check_once(now=1300.0) == []      # то же зависание
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ss._RUNNING.clear()

    assert [k for k, _ in alerts] == ["svc-stalled:hung"]
    assert "_hung_pass" in alerts[0][1]
    snap = metrics.snapshot()
    assert snap["gauges"]['infragram_service_stalled_seconds{service="hung"}'] >= 200
    assert snap["gauges"]['infragram_service_stalled_seconds{service="busy"}'] == 0
    assert snap["counters"]['infragram_service_stalls_total{service="hung"}'] == 1


async def test_finished_service_is_forgotten():
    ss._RUNNING.clear()

    async def _off():
        return None      # «выключен настройкой»

    await ss.supervise("off", _off)
    watch = ss.StallWatch(stall_after=1.0)
    assert await watch.check_once() == []
    assert "off" not in ss._RUNNING


def test_watch_is_started_in_every_role_and_metrics_described():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert ('_web_resilient(\n            "service_stall_watch", '
            'service_supervisor.run_stall_watch, _svc_alert)') in src
    for name in (ss.STALLED_SECONDS, ss.STALLS_TOTAL):
        assert name in metrics._HELP
