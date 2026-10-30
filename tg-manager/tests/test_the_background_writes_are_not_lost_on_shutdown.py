"""Фоновая запись доживает до конца, а не умирает вместе с процессом.

ЧТО ЗАЩИЩАЕМ. Побочные записи — телеметрия физики, журнал соответствия
(аудит-след, терять который нельзя), конверсии — запускаются через
`bg_tasks.spawn`: огонь-и-забыл, но с удержанием strong-ссылки, чтобы задачу не
собрал сборщик мусора.

Ссылка спасает от сборщика, но НЕ от конца процесса. Контейнер получает SIGTERM
на каждом деплое, и остановка шла так: воркер свернулся, пул закрылся, цикл
закрылся — а запущенные записи всё ещё висели в нём. Их не ждал никто, поэтому
они умирали на полпути: Python печатал «Task was destroyed but it is pending!»,
запись не ложилась, а закрытый поверх живой задачи пул ронял её запрос.

Теперь остановка их ЖДЁТ — коротко и ДО закрытия пула. Что не уложилось в
окно, отменяется громко и считается счётчиком: потерянная запись обязана быть
видимой.
"""
from __future__ import annotations

import ast
import asyncio
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DROPPED = "infragram_background_writes_dropped_total"


def _src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


def test_a_pending_write_is_awaited_not_dropped():
    from services import bg_tasks

    landed: list[str] = []

    async def _write():
        await asyncio.sleep(0.05)
        landed.append("аудит")

    async def _go():
        bg_tasks.spawn(_write(), name="аудит")
        # Остановка наступает ДО того, как запись успела лечь.
        assert landed == []
        return await bg_tasks.drain(2.0)

    res = asyncio.run(_go())
    assert landed == ["аудит"], "фоновая запись потерялась на остановке"
    assert res == {"done": 1, "cancelled": 0}, res


def test_a_write_that_never_finishes_is_cancelled_and_counted():
    """Ждать бесконечно нельзя: платформа добьёт контейнер, и уборка не успеет."""
    from services import bg_tasks

    async def _hangs():
        await asyncio.sleep(3600)

    async def _go():
        task = bg_tasks.spawn(_hangs(), name="вечная")
        res = await bg_tasks.drain(0.05)
        await asyncio.sleep(0)
        return res, task

    before = _total(_DROPPED)
    res, task = asyncio.run(_go())
    assert res == {"done": 0, "cancelled": 1}, res
    assert task.cancelled() or task.done(), "задача осталась висеть"
    assert _total(_DROPPED) > before, "потерянная запись не видна в метриках"


def test_draining_nothing_is_quiet_and_safe():
    from services import bg_tasks

    assert asyncio.run(bg_tasks.drain(0.01)) == {"done": 0, "cancelled": 0}


def test_the_shutdown_drains_before_it_closes_the_pool():
    """Порядок и есть смысл: после pool.close() записи уже некуда класть.

    Проверка по исходнику: `main()` поднимает бота и подключается к БД, в тесте
    его не вызвать, а молча выпавший вызов вернул бы ровно прежнюю потерю.
    """
    src = _src("main.py")
    tree = ast.parse(src)
    main_fn = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    body = ast.get_source_segment(src, main_fn) or ""
    assert "bg_tasks.drain(" in body, (
        "остановка больше не ждёт фоновые записи — они снова умрут на деплое")
    assert body.index("bg_tasks.drain(") < body.index("await pool.close()"), (
        "фоновые записи сворачиваются ПОСЛЕ закрытия пула — класть их уже некуда")
    assert body.index("op_worker.shutdown(") < body.index("bg_tasks.drain("), (
        "воркер обязан свернуться первым: его уборка сама пишет в базу")


def _orphan(bg_tasks) -> "object":
    """Оставить в наборе задачу из цикла, который больше не крутится.

    Именно так её оставляют три живых e2e-теста приглашений: `new_event_loop()
    .run_until_complete(...)`, и цикл дальше просто брошен. `asyncio.run` так
    не умеет — он на выходе отменяет оставшиеся задачи, и сироты не выходит;
    поэтому воспроизводить беду надо сырым циклом, иначе проверка зелёная и
    без фикса.
    """
    async def _never():
        await asyncio.sleep(3600)

    async def _go():
        return bg_tasks.spawn(_never(), name="сирота")

    loop = asyncio.new_event_loop()
    return loop, loop.run_until_complete(_go())


def _bury(loop, task) -> None:
    """Прибрать сироту после проверки: иначе сборщик печатает «Task was
    destroyed but it is pending!» на весь прогон — тот самый шум, от которого
    эта остановка и избавляет."""
    task.cancel()
    try:
        loop.run_until_complete(asyncio.sleep(0))
    except Exception:
        pass
    loop.close()


def test_a_write_from_an_abandoned_loop_is_not_counted_as_lost():
    """Отчёт остановки — про записи этого цикла; чужие в счётчик потерь не идут."""
    from services import bg_tasks

    loop, task = _orphan(bg_tasks)
    assert task in bg_tasks._bg_tasks, "сироту не удалось создать — проверка пуста"

    before = _total(_DROPPED)
    assert asyncio.run(bg_tasks.drain(0.01)) == {"done": 0, "cancelled": 0}, (
        "задача из брошенного цикла попала в отчёт остановки")
    assert _total(_DROPPED) == before, (
        "сирота посчитана потерянной записью — счётчик врёт")
    assert task not in bg_tasks._bg_tasks, (
        "сирота осталась в наборе strong-ссылок: набор растёт без предела")
    _bury(loop, task)


def test_an_orphan_does_not_stop_the_shutdown_from_waiting_for_its_own():
    """Отбор по циклу не должен заодно перестать ждать СВОИ записи."""
    from services import bg_tasks

    loop, task = _orphan(bg_tasks)
    landed: list[str] = []

    async def _write():
        await asyncio.sleep(0.05)
        landed.append("журнал")

    async def _go():
        bg_tasks.spawn(_write(), name="журнал")
        return await bg_tasks.drain(2.0)

    assert asyncio.run(_go()) == {"done": 1, "cancelled": 0}
    assert landed == ["журнал"], "своя запись потерялась из-за чужой"
    _bury(loop, task)


def test_the_counter_is_described_for_the_owner():
    from services.metrics import _HELP

    assert _DROPPED in _HELP
    assert "потерян" in _HELP[_DROPPED]
