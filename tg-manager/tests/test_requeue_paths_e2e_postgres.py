"""Все пути возврата операции в очередь — по НАСТОЯЩЕМУ Postgres.

Таких путей восемь, и каждый пишет свой UPDATE с `make_interval(secs => $N)`,
плавающей точкой в параметре и защитой от терминального статуса. Проверялись
они только по исходнику: заглушка пула принимает любые типы, поэтому ошибку
СВЯЗЫВАНИЯ она не видит в принципе — ровно так пережил релиз сломанный
`submit(scheduled_for=…)`, убивавший все отложенные операции продукта.

Здесь каждый путь выполняется на живой базе и проверяется по трём признакам,
которые делают возврат в очередь безопасным:

  * операция снова `pending` и `started_at` сброшен — иначе её не подхватит
    поллер, а сторож зависших посчитает её работающей;
  * `done_items=0` — иначе счётчик копится поверх прошлого прогона, и получается
    класс «done > total» с ложным «выполнено»;
  * `scheduled_for` в будущем — возврат без отсрочки означает мгновенный
    повторный заход в ту же стену (занятый флот, флуд-пауза, открытая цепь).

И отдельно — что ни один путь не переписывает ОТМЕНУ владельца: он нажал
«Отменить», операция в этот момент поймала флуд или сетевую ошибку, и возврат
в `pending` отправил бы массовую операцию вторым кругом после явного «стоп».

КАК ЗАПУСТИТЬ — рецепт в докстринге tests/test_op_finish_paths_e2e_postgres.py.
Без INFRAGRAM_TEST_DSN файл пропускается.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 990778

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

    async def _boot():
        # Пул создаём ВНУТРИ цикла: `create_pool(...)` запоминает текущий цикл
        # уже при конструировании объекта, и снаружи он привязывается не к тому,
        # в котором потом работает, — все тесты уходили в skip «Postgres
        # недоступен», хотя база была поднята.
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    # Схему накатывает соседний e2e-файл; здесь проверяем только наличие таблицы.
    try:
        _run(p.fetchval("SELECT 1 FROM operation_queue LIMIT 1"))
    except Exception as exc:
        pytest.skip(f"схема не накатана (запустите соседний e2e-файл): {str(exc)[:80]}")
    yield p
    _run(p.close())


async def _op(pool, status="running", **cols) -> int:
    keys = ["owner_id", "op_type", "params", "status", "done_items", "total_items"] + list(cols)
    vals = [OWNER, "mass_publish", json.dumps({}), status, 5, 10] + list(cols.values())
    ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
    return await pool.fetchval(
        f"INSERT INTO operation_queue({', '.join(keys)}) VALUES({ph}) RETURNING id",
        *vals)


async def _row(pool, op_id):
    return await pool.fetchrow(
        "SELECT status, done_items, started_at, last_error, "
        "       scheduled_for > now() AS in_future "
        "FROM operation_queue WHERE id=$1", op_id)


# Путь → как его вызвать на операции op_id.
def _paths():
    from services import op_worker as w
    return {
        "флуд-пауза": lambda pool, op_id: w._defer_op_for_flood(
            pool, op_id, 1800, "Telegram попросил подождать"),
        "занятый флот": lambda pool, op_id: w._requeue_op_no_accounts(
            pool, op_id, 90),
        "местный лимит владельца": lambda pool, op_id: w._release_op_for_owner_limit(
            pool, op_id),
        "открытая цепь": lambda pool, op_id: w._release_op_for_circuit(
            pool, op_id, 1800),
        "ошибка исполнителя": lambda pool, op_id: w._maybe_requeue(
            pool, op_id, RuntimeError("сеть"), {}, "mass_publish"),
    }


@pytest.mark.parametrize("name", sorted(_paths()))
def test_the_path_returns_the_operation_to_the_queue(pool, name):
    call = _paths()[name]

    async def _go():
        op_id = await _op(pool, started_at=await pool.fetchval("SELECT now()"))
        await call(pool, op_id)
        return await _row(pool, op_id)

    row = _run(_go())
    assert row["status"] == "pending", f"{name}: операция не вернулась в очередь"
    assert row["started_at"] is None, (
        f"{name}: started_at не сброшен — сторож зависших сочтёт её работающей")
    assert row["done_items"] == 0, (
        f"{name}: прогресс не сброшен — счётчик будет копиться поверх прошлого "
        f"прогона (класс «done > total»)")
    assert row["in_future"] is True, (
        f"{name}: нет отсрочки — операция тут же зайдёт в ту же стену")


@pytest.mark.parametrize("name", sorted(_paths()))
def test_the_path_never_overwrites_an_owner_cancellation(pool, name):
    call = _paths()[name]

    async def _go():
        op_id = await _op(pool, status="cancelled")
        await call(pool, op_id)
        return await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)

    assert _run(_go()) == "cancelled", (
        f"{name}: отмена владельца переписана — массовая операция пойдёт вторым "
        f"кругом после явного «стоп»")


def test_the_flood_path_explains_itself_to_the_owner(pool):
    """Молчание дороже сообщения: владелец начинает отменять и запускать заново."""
    async def _go():
        op_id = await _op(pool)
        from services import op_worker as w
        await w._defer_op_for_flood(pool, op_id, 1800, "Telegram попросил подождать")
        return await _row(pool, op_id)

    row = _run(_go())
    assert row["last_error"] and "Telegram" in row["last_error"], row["last_error"]


def test_the_fleet_path_gives_up_after_its_own_deadline(pool):
    """Ожидание свободного флота — единственный путь со своим пределом.

    Отсчёт идёт от СОЗДАНИЯ операции: ждать аккаунты бесконечно нельзя. Для
    флуд-паузы такой предел был бы наказанием за соблюдение правил, поэтому
    она живёт отдельным путём — проверяем, что пределы не перепутаны.
    """
    async def _go():
        from services import op_worker as w
        old = await pool.fetchval(
            f"SELECT now() - interval '{w._ACCT_WAIT_MAX_MIN + 10} minutes'")
        op_id = await _op(pool, acct_wait_since=old, created_at=old)
        await w._requeue_op_no_accounts(pool, op_id, 90)
        fleet = await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)
        op2 = await _op(pool, acct_wait_since=old, created_at=old)
        await w._defer_op_for_flood(pool, op2, 1800, "пауза Telegram")
        return fleet, await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op2)

    fleet, flood = _run(_go())
    assert fleet == "failed", "ожидание флота обязано иметь предел"
    assert flood == "pending", (
        "флуд-пауза провалила операцию по чужому пределу — это наказание за "
        "соблюдение правил платформы")


# ── Сторож зависших при старте воркера ───────────────────────────────────────
#
# Railway шлёт SIGTERM на каждом деплое, поэтому этот путь срабатывает в проде
# регулярно — и он же единственный, кто тратит бюджет живучести операции.

def test_startup_revives_a_stale_running_operation(pool):
    async def _go():
        from services import op_worker as w
        op_id = await _op(pool, status="running",
                          started_at=await pool.fetchval("SELECT now()"))
        await w._reset_stale_running(pool)
        return await pool.fetchrow(
            "SELECT status, done_items, started_at, revive_count "
            "FROM operation_queue WHERE id=$1", op_id)

    row = _run(_go())
    assert row["status"] == "pending", row
    assert row["started_at"] is None and row["done_items"] == 0
    assert row["revive_count"] == 1, "бюджет живучести не считается — операция, "\
        "роняющая воркер, будет подниматься вечно"


def test_startup_stops_reviving_a_poisonous_operation(pool):
    """Операция, роняющая воркер, обязана однажды получить терминальный статус."""
    async def _go():
        from services import op_worker as w
        op_id = await _op(pool, status="running", revive_count=w._MAX_REVIVES)
        await w._reset_stale_running(pool)
        return await pool.fetchrow(
            "SELECT status, error_msg FROM operation_queue WHERE id=$1", op_id)

    row = _run(_go())
    assert row["status"] == "failed", row
    assert row["error_msg"], "владелец должен понять, почему она больше не поднимается"


def test_startup_does_not_touch_a_cancelled_operation(pool):
    async def _go():
        from services import op_worker as w
        op_id = await _op(pool, status="cancelled")
        await w._reset_stale_running(pool)
        return await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)

    assert _run(_go()) == "cancelled"
