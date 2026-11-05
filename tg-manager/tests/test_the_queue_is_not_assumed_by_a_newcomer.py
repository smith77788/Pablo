"""Сбой запроса об аренде не делает новичка держателем очереди.

РАЗРЫВ. `replica_guard.acquire_executor_lease` при любом исключении возвращает
True — fail-open. Обоснование в докстринге говорит про лимиты: «без БД процесс
всё равно ничего не сделает, а пауза исполнителя из-за сетевого блипа
останавливает ВСЕ операции продукта — это дороже, чем маловероятный краткий
разъезд лимитов».

Для процесса, который аренду УЖЕ держит, это верно: блип не меняет того, кто
держатель, и останавливать живую работу из-за одного неудачного запроса
действительно дороже.

Для только что запустившегося процесса — нет, и цена совсем другая. Первое же
действие держателя аренды — `_reset_stale_running`, то есть
`UPDATE operation_queue SET status='pending', done_items=0 WHERE status='running'`.
Ровно от этого аренда и защищает (см. докстринг самого `replica_guard`:
«сторожа зависших операций возвращают в очередь всё, что висит в 'running', то
есть и живые операции соседа»). Значит, блип на ПЕРВОЙ попытке нового процесса
стоит не «разъезда лимитов», а перезапуска живых операций соседа: те же
приглашения тем же людям, второй пост в канал, обнулённый прогресс и
потраченная единица бюджета живучести. И случается это ровно тогда, когда
опаснее всего — на деплое, когда старый процесс ещё работает.

ЧЕМ ЛЕЧИТСЯ. Fail-open остаётся, но только для того, кто по своим последним
достоверным данным и есть держатель. Блип не меняет, кто держит аренду, —
значит, на сбое запроса процесс сохраняет последнюю известную правду о себе, а
не выдумывает её. Не знает про себя ничего — очередь не берёт и ждёт
следующего круга (10 с), что для этого случая и есть правильная цена.
"""
from __future__ import annotations

import asyncio

import pytest


class _Row(dict):
    pass


class _Pool:
    """Пул, который отдаёт заданный ответ на аренду либо падает."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.calls = 0

    async def fetchrow(self, *_a, **_kw):
        self.calls += 1
        ans = self._answers.pop(0) if self._answers else None
        if isinstance(ans, Exception):
            raise ans
        return ans

    async def execute(self, *_a, **_kw):
        return "UPDATE 1"


@pytest.fixture
def rg():
    from services import replica_guard
    # Вера процесса в свою аренду — состояние модуля; тесты не должны
    # протекать друг в друга.
    saved = getattr(replica_guard, "_lease_held_by", None)
    yield replica_guard
    if hasattr(replica_guard, "_lease_held_by"):
        replica_guard._lease_held_by = saved


def _counter(name: str) -> float:
    from services import metrics
    snap = metrics.snapshot().get("counters") or {}
    return float(sum(v for k, v in snap.items() if k.split("{")[0] == name))


def test_a_newcomer_does_not_assume_the_lease_when_the_query_fails(rg):
    """Новый процесс, не знающий про аренду ничего, на сбое очередь НЕ берёт."""
    pool = _Pool([RuntimeError("БД недоступна")])
    assert asyncio.run(rg.acquire_executor_lease(pool, "worker-новичок", "all")) is False, (
        "процесс, который аренду ни разу не получал, на сбое запроса объявил себя "
        "держателем — а значит сейчас сбросит живые операции соседа в очередь"
    )


def test_the_holder_keeps_working_when_the_query_fails(rg):
    """Самопроверка: держателя блип не останавливает — fail-open для него остаётся."""
    pool = _Pool([_Row(worker_id="worker-держатель"), RuntimeError("блип")])
    assert asyncio.run(rg.acquire_executor_lease(pool, "worker-держатель", "all")) is True
    assert asyncio.run(rg.acquire_executor_lease(pool, "worker-держатель", "all")) is True, (
        "держатель аренды остановился из-за одного неудачного запроса — это "
        "останавливает все операции продукта"
    )


def test_a_process_that_lost_the_lease_does_not_take_it_back_on_an_error(rg):
    """Проигравший аренду не возвращает её себе сбоем запроса."""
    pool = _Pool([_Row(worker_id="сосед"), RuntimeError("блип")])
    assert asyncio.run(rg.acquire_executor_lease(pool, "мы", "all")) is False
    assert asyncio.run(rg.acquire_executor_lease(pool, "мы", "all")) is False, (
        "процесс, которому аренду уже отказали, на сбое запроса решил, что она "
        "его — хотя очередь в этот момент разбирает другой"
    )


def test_the_planned_release_gives_up_the_belief_too(rg):
    """Отпустил аренду сам — больше не держатель, в том числе на сбое."""
    pool = _Pool([_Row(worker_id="мы"), RuntimeError("блип")])
    assert asyncio.run(rg.acquire_executor_lease(pool, "мы", "all")) is True
    asyncio.run(rg.release_executor_lease(pool, "мы"))
    assert asyncio.run(rg.acquire_executor_lease(pool, "мы", "all")) is False, (
        "процесс отпустил аренду при плановой остановке, но на сбое запроса "
        "снова считает её своей"
    )


@pytest.mark.parametrize("worker,answers", [
    ("новичок", [RuntimeError("БД недоступна")]),
    ("держатель", [_Row(worker_id="держатель"), RuntimeError("блип")]),
])
def test_the_failure_is_not_silent(rg, worker, answers):
    """Сбой запроса об аренде считается в обеих ветках — и в отказе, и в fail-open."""
    before = _counter("infragram_executor_lease_errors_total")
    pool = _Pool(answers)
    for _ in answers:
        asyncio.run(rg.acquire_executor_lease(pool, worker, "all"))
    assert _counter("infragram_executor_lease_errors_total") > before, (
        "сбой запроса об аренде исполнителя нигде не посчитан"
    )


def test_the_startup_cleanup_stays_behind_the_gate():
    """Самопроверка цепочки: уборка живых операций — только после взятия аренды.

    Если `_reset_stale_running` когда-нибудь окажется ПЕРЕД гейтом аренды, вся
    эта защита перестанет что-либо значить, а тесты выше будут по-прежнему
    зелёными: они проверяют только ответ `acquire_executor_lease`.
    """
    import ast
    import os

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "services", "op_worker.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    run = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "run"),
               None)
    assert run is not None, "в op_worker нет функции run"

    gate_line = None
    reset_line = None
    for node in ast.walk(run):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "acquire_executor_lease" and gate_line is None:
                gate_line = node.lineno
            if name == "_reset_stale_running" and reset_line is None:
                reset_line = node.lineno
    assert gate_line is not None, "в run нет гейта аренды исполнителя"
    assert reset_line is not None, "в run нет вызова _reset_stale_running"
    assert gate_line < reset_line, (
        "уборка зависших операций стоит ПЕРЕД гейтом аренды: процесс сбрасывает "
        "чужие живые операции в очередь, ещё не узнав, его ли это очередь"
    )
