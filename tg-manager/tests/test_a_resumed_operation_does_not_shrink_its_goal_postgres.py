"""Возобновлённый прогон не уменьшает цель операции — и 227 из 380 не «выполнено».

РАЗРЫВ. Исполнители ставят `total_items` по списку, с которым идут работать, а
на ВОЗОБНОВЛЁННОМ прогоне список короче: двери идемпотентности выбрасывают
цели, взятые прошлыми прогонами. Инвайт писал этот укороченный размер поверх
прежнего — цель операции уменьшалась под владельцем.

Чем это кончалось. 380 целей, первый прогон взял 177 и ушёл в повтор по
флуд-паузе. Второй прогон видит 203 новых, пишет `total_items=203`, берёт из
них 50 и упирается в суточные лимиты. На закрытии покрытие журнала (227 целей
за все прогоны) оказывается БОЛЬШЕ укороченной цели, проверка недобора
`done < total` не срабатывает — и операция, сделавшая 227 из 380, уходит
владельцу с зелёной галочкой. Ровно для этого случая существует 'partial'.

Разбор исхода опирается на то, что `total_items` — замах ПЕРВОГО прогона
(`_own_journal_coverage` написан про это), поэтому объявлять цель можно только
пока журнал операции пуст.

Нужен живой Postgres: проверяется связка «журнал → покрытие → классификация
исхода» настоящими запросами, а заглушка пула их не исполняет.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 993311

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER))
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


def _seed(pool, total: int, done: int = 0, journal: int = 0) -> int:
    op_id = int(_run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "                            done_items, label, started_at) "
        "VALUES($1,'mass_invite','running',$2,$3,$4,'goal test', now()) RETURNING id",
        OWNER, json.dumps({}), total, done)))
    for i in range(journal):
        _run(pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
            "VALUES($1,$2,$3,'ok','seed')", op_id, i, f"@t{i}"))
    return op_id


def _total(pool, op_id: int) -> int:
    return int(_run(pool.fetchval(
        "SELECT total_items FROM operation_queue WHERE id=$1", op_id)))


def test_a_resumed_run_keeps_the_goal_of_the_first_one(pool):
    """Журнал не пуст — значит это возобновление, и цель не переписываем."""
    from services import op_worker

    op_id = _seed(pool, total=380, done=0, journal=227)
    _run(op_worker._declare_total_items(pool, op_id, 203))

    assert _total(pool, op_id) == 380, (
        "возобновлённый прогон уменьшил цель операции: владелец ставил 380, "
        f"стало {_total(pool, op_id)} — и недобор перестаёт быть виден")


def test_the_first_run_still_declares_the_real_audience(pool):
    """Пробник: на первом прогоне заглушка мини-аппа обязана замениться.

    Без этого проверка выше была бы зелёной и у двери, которая не пишет
    вообще ничего.
    """
    from services import op_worker

    op_id = _seed(pool, total=1, done=0, journal=0)
    _run(op_worker._declare_total_items(pool, op_id, 280))

    assert _total(pool, op_id) == 280, (
        "первый прогон не объявил размер аудитории — полоса прогресса "
        f"осталась на заглушке: {_total(pool, op_id)}")


def test_the_invite_declares_its_goal_through_the_door():
    """Храповик: инвайт объявляет цель дверью, а не своим UPDATE.

    Собственный `UPDATE ... SET total_items` в исполнителе — это ровно то, что
    уменьшало цель на возобновлении.
    """
    import ast
    import inspect
    import re

    src = inspect.getsource(op_worker_module()._exec_mass_invite)
    assert "_declare_total_items(" in src, (
        "инвайт объявляет цель мимо общей двери — на возобновлении она снова "
        "уменьшится")
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"SET\s+total_items", node.value):
                raise AssertionError(
                    "в инвайте снова свой запрос, пишущий total_items: "
                    f"{node.value[:80]}")


def op_worker_module():
    from services import op_worker

    return op_worker


def test_the_shrunken_goal_is_what_called_a_partial_invite_done(pool):
    """Та же арифметика, что на закрытии: укороченная цель прячет недобор.

    Проверяем связку на настоящих данных: покрытие журнала 227 целей.
    При цели 380 закрытие обязано назвать это 'partial', при укороченной 203 —
    получается «выполнено», и ради этого дверь и существует.
    """
    from services import op_status, op_worker

    op_id = _seed(pool, total=380, done=50, journal=227)
    covered = _run(op_worker._own_journal_coverage(pool, op_id))
    assert covered == 227, f"покрытие журнала посчитано неверно: {covered}"

    honest_goal, shrunk_goal = 380, 203
    honest_done = honest_goal if covered >= honest_goal else 50
    shrunk_done = shrunk_goal if covered >= shrunk_goal else 50

    assert op_status.classify_final(
        "done", ok=50, failed=0,
        done_items=honest_done, total_items=honest_goal) == op_status.PARTIAL, (
        "недобор при честной цели обязан называться частичным")
    assert op_status.classify_final(
        "done", ok=50, failed=0,
        done_items=shrunk_done, total_items=shrunk_goal) == "done", (
        "сценарий не воспроизводится — проверка ниже ничего не значит")
