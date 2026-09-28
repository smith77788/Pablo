"""Зависшая операция отпускает слот и флот, не дожидаясь шестичасового потолка.

ЧТО БЫЛО. У прогона исполнителя стоял единственный ограничитель — потолок
времени (6 часов по умолчанию). Он отвечает на вопрос «не слишком ли долго»,
а нужен ответ на вопрос «жива ли вообще».

Массовой операции положено идти часами: это пейсинг против банов, а не болезнь.
Зависшая же — сеть легла, прокси молчит, вызов без собственного таймаута —
умирала на пятой минуте, но держала слот параллельности (один из восьми) и
арендованные аккаунты все шесть часов. Несколько таких, и у владельца почти не
остаётся ни флота, ни слотов на сутки, причём молча: по всем признакам операция
«выполняется».

ЧТО ТЕПЕРЬ. Признак «жива» исполнители и так пишут в БД — `done_items` растёт
после каждой взятой цели. Стоял счётчик дольше порога — операция не работает, и
прогон прерывается обычным путём: понятный текст владельцу, взятые цели
сохранены в журнале, повтор продолжает с того же места.

Сторож срабатывает ТОЛЬКО если операция уже что-то сделала: длинная подготовка
(разбор большой аудитории до первой цели) застоем не считается. Поэтому он
строго добавляет защиту и ничего не отнимает — раньше такая операция всё равно
дошла бы до потолка.
"""
from __future__ import annotations

import asyncio

import pytest


class _Pool:
    """Пул, отдающий заранее заданную последовательность значений done_items."""

    def __init__(self, progression):
        self.progression = list(progression)
        self.reads = 0

    async def fetchrow(self, query, *args):
        if "done_items" not in query:
            return None
        i = min(self.reads, len(self.progression) - 1)
        self.reads += 1
        val = self.progression[i]
        return None if val is None else {"done_items": val}

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        return "UPDATE 1"


@pytest.fixture
def fast(monkeypatch):
    """Ускоряем сторожа: опрос мгновенный, порог застоя — доли секунды."""
    from services import op_worker

    monkeypatch.setattr(op_worker, "_OP_STALL_POLL_S", 0.01, raising=False)
    monkeypatch.setattr(op_worker, "_OP_STALL_MIN", 0.05 / 60, raising=False)
    return op_worker


@pytest.mark.asyncio
async def test_stalled_operation_is_cut_off(fast):
    """Счётчик стоит — прогон прерывается, не дожидаясь потолка."""
    op_worker = fast
    started = asyncio.Event()

    async def _hangs_forever():
        started.set()
        await asyncio.sleep(3600)

    pool = _Pool([5, 5, 5, 5, 5, 5, 5, 5, 5, 5])
    with pytest.raises(asyncio.TimeoutError):
        await op_worker._run_with_stall_guard(
            pool, _hangs_forever(), 7, "bulk_join", timeout_s=3600)

    assert started.is_set(), "исполнитель даже не начал — проверка бессмысленна"


@pytest.mark.asyncio
async def test_a_slow_but_living_operation_is_not_touched(fast):
    """Счётчик движется медленно — работа здорова, трогать её нельзя."""
    op_worker = fast

    async def _slow():
        await asyncio.sleep(0.25)
        return {"ok": 3, "status": "done"}

    # done_items растёт на каждом опросе — операция жива, просто неспешна.
    pool = _Pool(range(1, 200))
    res = await op_worker._run_with_stall_guard(
        pool, _slow(), 7, "bulk_join", timeout_s=3600)

    assert res == {"ok": 3, "status": "done"}, (
        "сторож убил здоровую операцию, которая просто идёт медленно — "
        "массовым операциям положено идти часами"
    )


@pytest.mark.asyncio
async def test_long_preparation_before_the_first_target_is_not_a_stall(fast):
    """До первой взятой цели застоя не бывает: это подготовка, а не зависание."""
    op_worker = fast

    async def _prepares_then_returns():
        await asyncio.sleep(0.3)
        return {"ok": 1}

    pool = _Pool([0] * 200)          # счётчик стоит на нуле всё время
    res = await op_worker._run_with_stall_guard(
        pool, _prepares_then_returns(), 7, "mass_invite", timeout_s=3600)

    assert res == {"ok": 1}, (
        "разбор большой аудитории до первой цели принят за зависание"
    )


@pytest.mark.asyncio
async def test_unreadable_counter_never_kills_the_work(fast):
    """Сбой собственного зрения — не повод убивать операцию."""
    op_worker = fast

    async def _work():
        await asyncio.sleep(0.3)
        return {"ok": 2}

    pool = _Pool([None] * 200)       # счётчик не читается вовсе
    assert await op_worker._run_with_stall_guard(
        pool, _work(), 7, "bulk_join", timeout_s=3600) == {"ok": 2}


@pytest.mark.asyncio
async def test_absolute_ceiling_still_applies(fast):
    """Потолок времени никуда не делся: он ловит то, что двигается, но бесконечно."""
    op_worker = fast

    async def _endless():
        await asyncio.sleep(3600)

    pool = _Pool(range(1, 10000))    # счётчик всё время растёт — застоя нет
    with pytest.raises(asyncio.TimeoutError):
        await op_worker._run_with_stall_guard(
            pool, _endless(), 7, "bulk_join", timeout_s=0.2)


@pytest.mark.asyncio
async def test_executor_gets_a_chance_to_clean_up(fast):
    """Прерванный исполнитель обязан развернуть свои finally.

    Брошенная без ожидания задача продолжала бы держать аккаунты и сессии —
    ровно то, ради чего прогон и прерывают.
    """
    op_worker = fast
    cleaned = asyncio.Event()

    async def _hangs():
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.set()

    pool = _Pool([4] * 200)
    with pytest.raises(asyncio.TimeoutError):
        await op_worker._run_with_stall_guard(
            pool, _hangs(), 7, "bulk_join", timeout_s=3600)

    assert cleaned.is_set(), "исполнителя бросили, не дав освободить ресурсы"


def test_worker_runs_executors_through_the_guard():
    """Прогон обязан идти через сторожа, иначе защиты нет."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "services" / "op_worker.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    run_op = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_op_task"
    )
    calls = {
        c.func.id for c in ast.walk(run_op)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "_run_with_stall_guard" in calls, (
        "исполнитель вызывается мимо сторожа застоя: зависшая операция снова "
        "будет держать слот и флот до самого потолка"
    )
