"""Регрессия: сбой чтения журнала целей больше не оборачивается дублем действий.

РАЗРЫВ. Возобновлённый прогон (после флуд-паузы, рестарта на деплое, сброса
сторожем зависших) начинает список целей с начала — `done_items` обнуляется
намеренно. Не повторить уже сделанное — работа четырёх дверей идемпотентности:
`completed_targets`, `completed_account_targets`, `settled_targets`,
`completed_steps`. Все четыре читали журнал через `_safe_fetch`, а он на сбое
БД возвращает ПУСТОЙ список — «ни одна цель ещё не отработана».

То есть один сетевой блип к Postgres в момент возобновления означал: 380
адресатов получают второе одинаковое сообщение, каналы — второй пост, а люди из
аудитории — второе приглашение (самая палевная сигнатура и самая баноопасная
операция продукта). В логе оставался warning.

Теперь чтение журнала идёт отдельной дверью `_journal_fetch`: повтор запроса, а
если и он не прошёл — исключение. Классификатор ошибок относит его к 'retry',
поэтому операция уходит на обычный повтор с отсрочкой и доработает позже.
Отсрочка дешевле дубля.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os

import pytest

from services import op_worker
from services.op_errors import _classify_op_error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DOORS = (
    "completed_targets",
    "completed_account_targets",
    "settled_targets",
    "completed_steps",
)


class _Pool:
    """Пул, который падает на первых `fail` чтениях, а дальше отдаёт строки."""

    def __init__(self, fail: int, rows: list[dict] | None = None):
        self.fail = fail
        self.rows = rows or []
        self.reads = 0

    async def fetch(self, query, *args):
        self.reads += 1
        if self.reads <= self.fail:
            raise ConnectionResetError("БД отвалилась на середине чтения")
        return self.rows


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Повтор чтения ждёт полсекунды — в тесте ждать нечего."""
    async def _fast(_s):
        return None
    monkeypatch.setattr(op_worker.asyncio, "sleep", _fast)


@pytest.fixture
def counted(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        op_worker, "_reliability_metric",
        lambda name, n=1, **lbl: seen.append((name, lbl.get("where"))))
    return seen


def test_a_dead_journal_stops_the_run_instead_of_repeating_it(counted):
    """Не прочитали журнал — отказываемся работать, а не считаем, что дел нет."""
    pool = _Pool(fail=2)
    with pytest.raises(op_worker.JournalUnreadable):
        _run(op_worker.completed_targets(pool, 777))
    assert pool.reads == 2, (
        f"блип обязан лечиться повтором, а не одним выстрелом: чтений {pool.reads}")
    assert ("infragram_journal_read_failures_total", "completed_targets") in counted, (
        f"отказ не оставил следа на графике: {counted}")


def test_the_refusal_sends_the_operation_to_a_retry_not_to_a_failure():
    """Последствие, а не только исключение: операция обязана уйти на повтор.

    Классификатор смотрит на текст и имя класса. Слово из фатального набора в
    формулировке — и операция закрылась бы провалом, то есть владелец получил
    бы «не вышло» там, где достаточно было подождать.
    """
    exc = op_worker.JournalUnreadable("журнал целей недоступен, прогон отложен до повтора")
    assert _classify_op_error(exc) == "retry", (
        "отказ читать журнал закрывает операцию насовсем вместо повтора")


def test_a_single_blip_is_healed_by_the_retry():
    """Один сбой из двух — не повод откладывать: прогон продолжается как обычно."""
    pool = _Pool(fail=1, rows=[{"target": "@ivan"}])
    assert _run(op_worker.completed_targets(pool, 777)) == {"@ivan"}
    assert pool.reads == 2


def test_the_healthy_path_is_unchanged():
    """Журнал читается — двери отвечают тем же, чем раньше."""
    pool = _Pool(fail=0, rows=[{"target": "@ivan"}, {"target": "@olga"}])
    assert _run(op_worker.completed_targets(pool, 777)) == {"@ivan", "@olga"}
    assert pool.reads == 1


def test_every_idempotency_door_reads_through_the_closed_one():
    """Чтобы следующая дверь не завелась на `_safe_fetch` по привычке.

    Проверяем не текст окна, а тело каждой функции: окно съезжает от любой
    правки рядом, а тело — нет.
    """
    src = inspect.getsource(op_worker)
    tree = ast.parse(src)
    bodies = {
        n.name: ast.get_source_segment(src, n)
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    for door in _DOORS:
        body = bodies.get(door)
        assert body, f"дверь идемпотентности {door} исчезла"
        assert "_journal_fetch(" in body, (
            f"{door} читает журнал обычным чтением: сбой БД обернётся повтором "
            "уже сделанных действий")
        assert "_safe_fetch(" not in body, (
            f"{door} оставила за собой fail-open чтение журнала")


# ── Та же дыра с другой стороны: пропавшая отметка «сделано» ────────────────


class _WritePool:
    """Пул, который падает на первых `fail` записях, а дальше пишет."""

    def __init__(self, fail: int):
        self.fail = fail
        self.writes = 0

    async def execute(self, query, *args):
        self.writes += 1
        if self.writes <= self.fail:
            raise ConnectionResetError("БД отвалилась на записи")
        return "INSERT 0 1"


def test_a_lost_mark_is_retried_and_then_said_out_loud(counted):
    """Отметка «цель сделана» пропала — об этом обязан знать график.

    Запись идёт ПОСЛЕ доставки: цель уже отработана, отменять нечего, падать
    нельзя (потеряли бы остаток прогона). Но пропавшая отметка означает, что
    возобновлённый прогон сделает эту цель второй раз, а это второе сообщение
    живому человеку. Раньше оставался только warning в логе.
    """
    pool = _WritePool(fail=2)
    res = _run(op_worker._journal_write(
        pool, "INSERT INTO operation_log(op_id, target, status) VALUES($1,$2,'ok')",
        777, "@ivan", log_ctx="[t]", where="bulk_dm"))
    assert res == "ERROR", "дверь обязана отвечать тем же, чем прежняя запись"
    assert pool.writes == 2, f"блип обязан лечиться повтором: записей {pool.writes}"
    assert ("infragram_journal_write_failures_total", "bulk_dm") in counted, (
        f"риск дубля не виден на графике: {counted}")


def test_a_single_blip_on_the_write_is_healed():
    """Один сбой из двух — отметка всё-таки записана, дубля не будет."""
    pool = _WritePool(fail=1)
    assert _run(op_worker._journal_write(pool, "INSERT INTO operation_log", 1)) == "INSERT 0 1"
    assert pool.writes == 2


def test_no_journal_write_silently_swallows_a_failure():
    """Храповик: новая запись в журнал не завелась на молчаливом глотателе.

    Проверяем по AST, а не по тексту окна: записей в журнал больше пятидесяти,
    и любая новая, сделанная «как рядом», вернула бы дыру целиком.
    """
    src = inspect.getsource(op_worker)
    tree = ast.parse(src)
    swallowed = []
    through_door = 0
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
            continue
        q = next((a.value for a in n.args
                  if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
        if "INSERT INTO operation_log" not in q:
            continue
        if n.func.id == "_safe_execute":
            swallowed.append(n.lineno)
        elif n.func.id == "_journal_write":
            through_door += 1
    assert not swallowed, (
        "записи в журнал целей через молчаливое _safe_execute (строки "
        f"{swallowed}): пропавшая отметка обернётся повтором уже сделанного")
    assert through_door >= 30, (
        f"через дверь журнала идёт всего {through_door} записей — похоже, "
        "пробник перестал их видеть, а не код починился")
