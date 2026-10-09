"""Отмена запущенной операции доводится до конца, даже если воркер умер.

РАЗРЫВ. Отмена — одна дверь, `operation_bus.cancel`. Для ОЖИДАЮЩЕЙ операции
она дописывает итог сама (`_close_cancelled_pending`), а для ЗАПУЩЕННОЙ —
сознательно не дописывает: её закроет `op_worker._finish_cancelled_op`, когда
исполнитель увидит 'cancelled'. Это прямо написано и в коде двери, и в
докстринге `tests/test_cancel_has_one_door.py`.

Допущение держится только пока процесс жив. Railway шлёт SIGTERM на каждом
деплое (пуш в рабочую ветку = прод), и между нажатием «Отменить» и моментом,
когда исполнитель дойдёт до проверки отмены, процесса может уже не быть:
исполнитель сидит во флуд-паузе, проверка отмены кэшируется, задача живёт
своей жизнью. Тогда операция остаётся терминальной, но ПУСТОЙ:

  * `result IS NULL` — владелец видит «Отменено» без единой цифры. Именно эта
    цифра отвечает на главный вопрос после отмены: сколько приглашений уже
    ушло, то есть продолжать ли и с какого места;
  * исхода нет на графике `infragram_operations_total`;
  * в подписанном аудит-трейле (`compliance_engine`) операции нет;
  * память организма не получает `op_done`.

И починить это нечем: `_reset_stale_running` при старте трогает только
'running', сторож зависших — тоже. Отменённая-на-ходу операция не чинится ни
рестартом, ни временем.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяется семантика запроса-уборщика (какие именно
строки под него попадают: возраст, активные в памяти, уже закрытые) и то, что
цифры берутся из журнала целей. Заглушка пула SQL не исполняет вовсе — на ней
«попала строка или нет» не проверяется в принципе. Рецепт запуска — в
docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991778
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
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER))
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))


async def _abandoned(
    pool, *, ok_targets: int = 3, failed_targets: int = 1,
    cancelled_min_ago: int = 30, ran_for_min: int = 10,
    result: dict | None = None,
) -> int:
    """Операция, отменённая на ходу, закрыть которую уже некому."""
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, "
        "total_items, done_items) "
        "VALUES($1,'mass_invite',$2::jsonb,'cancelled',10,$3) RETURNING id",
        OWNER, json.dumps({"account_ids": [7]}), ok_targets)
    await pool.execute(
        "UPDATE operation_queue "
        "   SET started_at = NOW() - make_interval(mins => $2::int + $3::int), "
        "       finished_at = NOW() - make_interval(mins => $2), "
        "       result = $4::jsonb "
        " WHERE id = $1",
        op_id, int(cancelled_min_ago), int(ran_for_min),
        json.dumps(result) if result is not None else None)
    for i in range(ok_targets):
        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
            "VALUES($1, $2, $3, 'ok', 'приглашён')", op_id, i + 1, f"uid#{i}")
    for i in range(failed_targets):
        await pool.execute(
            "INSERT INTO operation_log(op_id, step_num, target, status, message) "
            "VALUES($1, $2, $3, 'error', 'отказ')",
            op_id, ok_targets + i + 1, f"uid#bad{i}")
    return op_id


async def _result(pool, op_id: int) -> dict | None:
    raw = await pool.fetchval(
        "SELECT result FROM operation_queue WHERE id=$1", op_id)
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _sweep(pool):
    from services import op_worker
    return _run(op_worker._watchdog_cancelled_orphans(pool))


# ── Главное: цифры доходят до владельца ──────────────────────────────────────

def test_an_abandoned_cancelled_operation_gets_its_numbers(pool):
    """Без этого владелец видит «Отменено» и ноль цифр — навсегда."""
    op_id = _run(_abandoned(pool))
    assert _run(_result(pool, op_id)) is None, "подготовка теста неверна"

    _sweep(pool)

    res = _run(_result(pool, op_id))
    assert res is not None, (
        "отменённая на ходу операция осталась без итога — ни цифр владельцу, "
        "ни исхода на графике, ни записи в аудит-трейле")
    assert res["ok"] == 3 and res["failed"] == 1, (
        f"цифры не из журнала целей: {res}")
    assert res["status"] == "cancelled", "подменён терминальный статус"
    assert res["summary"], "итог без слов для владельца"


def test_the_real_duration_is_written_not_zero(pool):
    """«Сколько шла» — половина ответа на «успело ли что-нибудь уйти»."""
    op_id = _run(_abandoned(pool, ran_for_min=10))
    _sweep(pool)
    res = _run(_result(pool, op_id))
    assert res and res["duration_s"] > 500, (
        f"длительность не посчитана по started_at..finished_at: {res}")


def test_the_status_stays_terminal(pool):
    """Уборщик дописывает итог, а не воскрешает операцию."""
    op_id = _run(_abandoned(pool))
    _sweep(pool)
    assert _run(pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)) == "cancelled"


# ── Чего уборщик делать не должен ────────────────────────────────────────────

def test_a_freshly_cancelled_operation_is_left_alone(pool):
    """Исполнитель закроет её сам в течение секунд — иначе итог объявят дважды.

    Щель реальна: поллер выбирает строку из очереди ДО того, как запишет op_id
    в `_active_op_ids`, и отмена, попавшая ровно в эту щель, не видна уборщику
    как активная.
    """
    op_id = _run(_abandoned(pool, cancelled_min_ago=0))
    _sweep(pool)
    assert _run(_result(pool, op_id)) is None, (
        "уборщик закрыл только что отменённую операцию — её исход объявится "
        "второй раз, когда исполнитель свернётся")


def test_an_operation_running_in_this_process_is_left_alone(pool):
    """Тот же критерий, по которому её щадят оба сторожа зависших."""
    from services import op_worker

    op_id = _run(_abandoned(pool, cancelled_min_ago=30))
    op_worker._active_op_ids.add(int(op_id))
    try:
        _sweep(pool)
    finally:
        op_worker._active_op_ids.discard(int(op_id))
    assert _run(_result(pool, op_id)) is None, (
        "закрыли операцию, которая выполняется в этом процессе прямо сейчас")


def test_an_operation_that_already_has_a_result_is_not_touched(pool):
    """Иначе исход объявлялся бы заново на каждом круге сторожа."""
    op_id = _run(_abandoned(pool, result={"status": "cancelled", "ok": 99,
                                          "failed": 0, "summary": "своё"}))
    _sweep(pool)
    res = _run(_result(pool, op_id))
    assert res and res["ok"] == 99, f"итог перезаписан уборщиком: {res}"


def test_other_terminal_statuses_are_not_touched(pool):
    """Провал без итога — другой класс и другой путь закрытия."""
    op_id = _run(_abandoned(pool))
    _run(pool.execute(
        "UPDATE operation_queue SET status='failed' WHERE id=$1", op_id))
    _sweep(pool)
    assert _run(_result(pool, op_id)) is None, (
        "уборщик отмены трогает чужие статусы")


# ── Самопроверка пробника и связка с циклом ──────────────────────────────────

def test_the_sweep_is_wired_into_the_watchdog_cycle():
    """Функция, которую никто не зовёт, не чинит ничего."""
    src = open("services/op_worker.py", encoding="utf-8").read()
    run_body = src[src.index("async def run("):]
    run_body = run_body[:run_body.index("\nasync def shutdown(")]
    assert "_watchdog_cancelled_orphans(" in run_body, (
        "уборщик брошенных отмен не вызывается из цикла воркера")


def test_the_sweep_goes_through_the_single_finish_door():
    """Половина итога теряется ровно тогда, когда закрытие пишут вручную."""
    src = open("services/op_worker.py", encoding="utf-8").read()
    body = src[src.index("async def _watchdog_cancelled_orphans("):]
    body = body[:body.index("\nasync def ", 10)]
    assert "_finish_cancelled_op(" in body, (
        "уборщик закрывает операцию в обход единственной двери закрытия отмены")
    assert "UPDATE operation_queue SET status" not in body, (
        "уборщик пишет статус сам — это и есть обход двери")


def test_the_probe_would_notice_a_sweep_that_does_nothing(pool):
    """Проверка измерителя: тесты выше обязаны падать, если уборщик пуст."""
    from services import op_worker

    op_id = _run(_abandoned(pool))
    calls: list = []

    async def _noop(*a, **k):
        calls.append(a)

    real = op_worker._finish_cancelled_op
    op_worker._finish_cancelled_op = _noop
    try:
        _sweep(pool)
    finally:
        op_worker._finish_cancelled_op = real
    assert calls, "уборщик не дошёл до закрытия подходящей операции"
    assert _run(_result(pool, op_id)) is None, (
        "итог появился в обход двери закрытия")
