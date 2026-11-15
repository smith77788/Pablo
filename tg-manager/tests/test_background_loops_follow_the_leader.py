"""Горизонтальный исполнитель: фоновые циклы идут только у лидера.

При OP_SHARDED_EXECUTOR=1 операции разбирают все процессы-исполнители, а
фоновые циклы (прогрев, призрак, восстановление, авто-ответы, расписания…)
обязаны идти в ОДНОМ экземпляре: каждый лишний процесс иначе удваивал бы
реальные действия аккаунтов. `supervise_while_leader` поднимает сервис, когда
процесс стал лидером, гасит при потере лидерства и поднимает снова при
возврате. main.py обязан направлять туда всё, кроме сервисов памяти процесса.
"""
from __future__ import annotations

import asyncio
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_service_runs_only_while_leader():
    from services import service_supervisor as ss

    leader = {"v": False}
    starts: list[int] = []
    stops: list[int] = []

    async def svc():
        starts.append(1)
        try:
            while True:
                await asyncio.sleep(0.01)
        finally:
            stops.append(1)

    async def go():
        t = asyncio.create_task(ss.supervise_while_leader(
            "t", svc, is_leader=lambda: leader["v"], poll_s=0.02))
        await asyncio.sleep(0.1)
        assert starts == [], "сервис поднялся в процессе, который не лидер"
        leader["v"] = True
        await asyncio.sleep(0.1)
        assert len(starts) == 1
        leader["v"] = False
        await asyncio.sleep(0.1)
        assert len(stops) == 1, "после потери лидерства сервис продолжал работать"
        leader["v"] = True
        await asyncio.sleep(0.1)
        assert len(starts) == 2, "вернувшееся лидерство не подняло сервис снова"
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


def test_disabled_service_ends_the_wrapper():
    from services import service_supervisor as ss

    async def svc():
        return None   # выключен настройкой

    async def go():
        await asyncio.wait_for(ss.supervise_while_leader(
            "t", svc, is_leader=lambda: True, poll_s=0.01), timeout=2)

    asyncio.run(go())


def test_main_gates_background_loops_by_leadership():
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("async def _resilient("):src.index("async def _web_resilient(")]
    assert "supervise_while_leader" in body and "sharded_executor()" in body
    assert "_PER_PROCESS_SERVICES" in body
    import re
    m = re.search(r"_PER_PROCESS_SERVICES = frozenset\(\{(.*?)\}\)", src, re.S)
    names = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    # исполнитель обязан работать в каждом процессе — ради этого всё и делается
    assert "op_worker" in names
    # и ни одного процесса с реальными действиями аккаунтов в «каждом процессе»
    assert not names & {"account_warmer", "ghost_engine", "auto_responder",
                        "scheduler", "chat_warmup", "behavioral_engine"}
