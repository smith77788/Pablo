"""Случайное значение в params выключало дедуп по всему типу операции.

ЧТО БЫЛО. Пакетное применение оформления к проекту (`_submit_bulk`,
op_type `gp_bulk_apply`) кладёт в params `seed_shift = random.randrange(...)`.
Сдвиг там нужен по делу: без него повтор нарисовал бы те же аватарки и выдал
бы те же кандидаты username. Но окно идемпотентности `operation_bus.submit`
сравнивает params, поэтому у двух тапов они не совпадали НИКОГДА — дедуп по
этому типу был слеп полностью, и выглядело это как рабочая защита.

ЦЕНА. Два тапа «Аватар» (или «Оформление») — два прохода по профилям всех
каналов проекта: правка названия, описания и аватарки каждому по второму разу.
Это необратимо, видно снаружи и удваивает самую заметную автоматическую
активность на тех же аккаунтах.

ЧТО ЗАКРЕПЛЕНО. Переменные ключи (`DEDUP_VOLATILE_KEYS`) вычёркиваются из
личности операции, но ОСТАЮТСЯ в params: исполнителю сдвиг нужен, а намеренный
повтор позже окна получает свой новый сдвиг и перерисовывает честно.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993619

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    yield


def _submit(pool, *, seed: int, action: str = "avatar", plan_id: int = 7707) -> int:
    from services import operation_bus

    return _run(operation_bus.submit(
        pool, OWNER, "gp_bulk_apply",
        {"plan_id": plan_id, "action": action, "seed_shift": seed},
        total_items=12, bypass_plan_check=True))


def test_two_taps_with_their_own_seeds_are_one_operation(pool):
    first = _submit(pool, seed=111111)
    second = _submit(pool, seed=222222)     # второй тап: свой случайный сдвиг
    assert second == first, (
        "второй тап поставил ВТОРУЮ операцию — профили всех каналов проекта "
        "будут переписаны дважды")
    assert int(_run(pool.fetchval(
        "SELECT count(*) FROM operation_queue WHERE owner_id=$1", OWNER))) == 1


def test_the_executor_still_gets_its_seed(pool):
    """Вычеркнут из личности — но не из params: иначе повтор перерисовал бы то же."""
    op_id = _submit(pool, seed=333333)
    params = _run(pool.fetchval(
        "SELECT params FROM operation_queue WHERE id=$1", op_id))
    import json

    stored = json.loads(params) if isinstance(params, str) else params
    assert int(stored["seed_shift"]) == 333333, stored


def test_a_different_action_is_a_different_operation(pool):
    """Обратная сторона: вычёркивание не должно склеивать разные операции."""
    avatar = _submit(pool, seed=444444, action="avatar")
    pinned = _submit(pool, seed=444444, action="pin")
    assert pinned != avatar, (
        "закрепление поста слили с перерисовкой аватарок: вторая операция "
        "тихо не выполнится вовсе")


def test_a_different_project_is_a_different_operation(pool):
    first = _submit(pool, seed=555555, plan_id=7707)
    other = _submit(pool, seed=555555, plan_id=7708)
    assert other != first, "оформление другого проекта слили с этим"


def test_the_window_of_this_type_outlives_the_client_timeout():
    """Окно короче клиентского таймаута не покрывает повтор после «ошибки»."""
    from services import operation_bus

    meta = operation_bus.OP_REGISTRY["gp_bulk_apply"]
    assert int(meta.get("dedup_window_sec") or 0) >= 60, (
        "клиент ждёт ответа 30 секунд, и ещё минута уходит на то, чтобы "
        "владелец увидел ошибку и нажал заново")


def test_the_volatile_key_is_not_struck_out_everywhere():
    """Вычёркиваются ИМЕННО перечисленные ключи, а не всё подряд."""
    from services import operation_bus

    src = {"plan_id": 1, "action": "avatar", "seed_shift": 42,
           "_progress_message_id": 9}
    assert operation_bus._dedup_identity_params(src) == {
        "plan_id": 1, "action": "avatar"}
    assert "seed_shift" in operation_bus.DEDUP_VOLATILE_KEYS
