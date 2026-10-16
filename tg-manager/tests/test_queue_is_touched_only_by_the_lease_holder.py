"""Сторожа и разбор очереди — только у держателя аренды исполнителя.

ЧТО ЗАЩИЩАЕМ. Вторая реплика с ролью worker опасна не только разъездом лимитов
флуда (они живут в памяти процесса). Опаснее сторожа: `_reset_stale_running` на
старте и `_watchdog_stale` каждую минуту возвращают в очередь ВСЁ, что висит в
'running'. Для второго процесса живые операции первого выглядят жертвами
падения: первый пошёл бы делать их второй раз — реальные приглашения, посты,
сообщения людям, — а бюджет живучести списался бы впустую, и здоровая
многочасовая операция объявлялась бы ядовитой.

Поэтому порядок в `run` важен сам по себе: аренда берётся ПЕРВЫМ действием круга,
и без неё круг заканчивается `continue`. Проверяем именно порядок по узлам AST, а
не наличие текста: вызов аренды рядом с незащищённым разбором очереди — это тот
же дефект.

op_worker импортирует telethon и в тестовой среде не поднимается — связку
проверяем по исходнику, как и соседние тесты очереди.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")

# Что нельзя делать без аренды.
GUARDED_CALLS = (
    "_process_pending",
    "_reset_stale_running",
    "_watchdog_stale",
    "_reconcile_in_operation",
    # Уборщик брошенных отмен дописывает итог в очередь — тот же класс: без
    # аренды он закрыл бы операцию, которую прямо сейчас ведёт держатель.
    "_watchdog_cancelled_orphans",
    # Сторож оборвавшихся расписаний ставит следующий круг через шину и
    # помечает закрытый круг — обе записи в очередь владельца.
    "_watchdog_recurring_chain",
)


def _run_fn(src: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            return node
    raise AssertionError("в op_worker нет `async def run` — цикл воркера переименован?")


def _lease_gate(loop: ast.While) -> ast.If:
    """Ветка «аренды нет → круг кончается», первая в теле цикла."""
    for st in loop.body:
        if not isinstance(st, ast.If):
            continue
        test = ast.dump(st.test)
        if "acquire_executor_lease" not in test:
            continue
        if any(isinstance(x, ast.Continue) for x in ast.walk(st)):
            return st
    raise AssertionError(
        "в цикле `run` нет ветки «аренда не наша → continue»: процесс без аренды "
        "будет разбирать очередь и водить сторожей по чужим живым операциям")


def _calls_before(node: ast.AST, line: int) -> list[tuple[str, int]]:
    out = []
    for x in ast.walk(node):
        if not isinstance(x, ast.Call):
            continue
        name = getattr(x.func, "id", None) or getattr(x.func, "attr", None)
        if name in GUARDED_CALLS and x.lineno < line:
            out.append((name, x.lineno))
    return out


@pytest.fixture(scope="module")
def src() -> str:
    with open(SRC, encoding="utf-8") as f:
        return f.read()


def test_lease_is_taken_before_anything_touches_the_queue(src):
    fn = _run_fn(src)
    loops = [x for x in fn.body if isinstance(x, ast.While)]
    assert loops, "цикл опроса очереди в `run` пропал"
    gate = _lease_gate(loops[0])
    early = _calls_before(fn, gate.lineno)
    assert not early, (
        "эти вызовы идут ДО проверки аренды — процесс без аренды успеет тронуть "
        "чужие живые операции: "
        + ", ".join(f"{n} (строка {ln})" for n, ln in early))


def test_the_detector_notices_an_ungated_loop():
    """Самопроверка: на заведомо больном коде детектор обязан находить дефект.

    Правило CLAUDE.md — измеритель проверяется на больном и здоровом примере,
    иначе его зелёный цвет ничего не значит.
    """
    sick = (
        "async def run(pool, bot):\n"
        "    await _reset_stale_running(pool, bot)\n"
        "    while True:\n"
        "        if not await _rguard.acquire_executor_lease(pool, w, r):\n"
        "            continue\n"
        "        await _process_pending(pool, bot)\n"
    )
    fn = _run_fn(sick)
    gate = _lease_gate([x for x in fn.body if isinstance(x, ast.While)][0])
    assert _calls_before(fn, gate.lineno), "детектор не увидел уборку до аренды"

    healthy = (
        "async def run(pool, bot):\n"
        "    while True:\n"
        "        if not await _rguard.acquire_executor_lease(pool, w, r):\n"
        "            continue\n"
        "        await _reset_stale_running(pool, bot)\n"
        "        await _process_pending(pool, bot)\n"
    )
    fn = _run_fn(healthy)
    gate = _lease_gate([x for x in fn.body if isinstance(x, ast.While)][0])
    assert not _calls_before(fn, gate.lineno), "детектор нашёл дефект в здоровом коде"

    no_gate = (
        "async def run(pool, bot):\n"
        "    while True:\n"
        "        await _process_pending(pool, bot)\n"
    )
    with pytest.raises(AssertionError):
        _lease_gate([x for x in _run_fn(no_gate).body if isinstance(x, ast.While)][0])


def test_planned_stop_releases_the_lease(src):
    """Иначе очередь стоит до истечения срока после КАЖДОГО деплоя."""
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "shutdown"), None)
    assert fn is not None, "в op_worker нет `async def shutdown`"
    names = {getattr(x.func, "attr", None) for x in ast.walk(fn) if isinstance(x, ast.Call)}
    assert "release_executor_lease" in names, (
        "плановая остановка не отпускает аренду: следующий процесс ждёт её срок, "
        "то есть очередь встаёт на каждом деплое")


def test_lease_failure_does_not_stop_the_queue():
    """Сбой запроса аренды — fail-open: без БД процесс всё равно ничего не сделает,
    а пауза исполнителя из-за сетевого блипа останавливает ВСЕ операции продукта."""
    import asyncio

    from services import replica_guard as rg

    class _Broken:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("соединение потеряно")

    assert asyncio.run(rg.acquire_executor_lease(_Broken(), "host:1:aaa", "worker")) is True


def test_lease_denied_is_not_a_silent_pause(src):
    """Пауза обязана быть видна: лог и счётчик, иначе «операции не идут» без причины."""
    fn = _run_fn(src)
    gate = _lease_gate([x for x in fn.body if isinstance(x, ast.While)][0])
    body = ast.dump(gate)
    assert "infragram_executor_lease_denied_total" in body, "у паузы нет счётчика"
    assert "warning" in body, "у паузы нет записи в лог"
