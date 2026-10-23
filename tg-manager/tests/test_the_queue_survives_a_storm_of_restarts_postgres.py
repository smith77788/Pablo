"""Очередь выдерживает шторм рестартов, не ломая свой инвариант.

ЗАЧЕМ ИМЕННО ШТОРМ. Ветка едет на Railway, пуш в неё означает прод, и четыре
агента выпускают по нескольку раз в час: операция живёт дольше, чем интервал
между деплоями. Все пути состояния по отдельности проверены точечными тестами
(пауза, отмена, сброс зависшей, бюджет живучести, уборка по SIGTERM). Этот тест
проверяет другое: что ЛЮБОЕ их чередование оставляет очередь в осмысленном
состоянии. Такие дыры не находятся тестом на один путь — только на их смеси.

ИНВАРИАНТЫ взяты из обещаний самого кода, а не придуманы здесь:

  * `done_items <= total_items` — комментарии op_worker называют это «классом
    done > total»: возврат в очередь обнуляет счётчик, чтобы прогресс не копился
    поверх прошлого прогона и владелец не видел «34/17»;
  * у операции в 'pending' не бывает `started_at` — его снимают все пути
    возврата в очередь (иначе сторож зависших посчитает её идущей и сбросит, а
    бюджет живучести потратится впустую);
  * у операции в 'running' `started_at` есть — на нём стоит весь расчёт
    зависших;
  * `revive_count` не превышает бюджет больше чем на один шаг: за ним операция
    обязана уйти в 'failed', а не крутиться вечно, забирая слот и флот.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяются ровно те запросы, которыми очередь и
живёт: атомарный захват (FOR UPDATE SKIP LOCKED), сброс зависших, гашение по
бюджету. Заглушка пула SQL не исполняет вовсе. Рецепт запуска —
в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import random
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 993377
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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass          # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


async def _forget_active_state(ow) -> None:
    async with ow._active_lock:
        ow._active_op_ids.clear()
    ow._shutting_down = False


# ── инварианты ──────────────────────────────────────────────────────────────

async def _violations(pool) -> list[str]:
    bad: list[str] = []
    rows = await pool.fetch(
        "SELECT id, status, started_at, done_items, total_items, "
        "COALESCE(revive_count,0) AS revives FROM operation_queue "
        "WHERE owner_id=$1", OWNER)
    from services import op_worker as ow

    for r in rows:
        if (r["done_items"] or 0) > (r["total_items"] or 0):
            bad.append(f"op={r['id']}: done_items {r['done_items']} > "
                       f"total_items {r['total_items']} — владелец видит «34/17»")
        if r["status"] == "pending" and r["started_at"] is not None:
            bad.append(f"op={r['id']}: в 'pending', но со started_at — сторож "
                       "зависших посчитает её идущей и потратит бюджет живучести")
        if r["status"] == "running" and r["started_at"] is None:
            bad.append(f"op={r['id']}: в 'running' без started_at — расчёт "
                       "зависших её не увидит никогда")
        if r["revives"] > ow._MAX_REVIVES + 1:
            bad.append(f"op={r['id']}: revive_count {r['revives']} превысил "
                       f"бюджет {ow._MAX_REVIVES} — операция крутится вечно, "
                       "забирая слот и флот")
    return bad


async def _seed(pool, n: int) -> list[int]:
    ids = []
    for i in range(n):
        ids.append(await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, params, status, "
            "total_items, done_items) "
            "VALUES($1,'mass_publish',$2::jsonb,'pending',10,0) RETURNING id",
            OWNER, json.dumps({"i": i})))
    return ids


def test_any_interleaving_of_restarts_keeps_the_queue_sane(pool):
    """Случайная смесь подхватов, падений, уборок и стартов — инвариант цел."""
    from services import op_worker as ow

    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(_seed(pool, 12))

    started: list[int] = []

    async def _fake_task(_pool, _bot, row):
        """Подхват без исполнителя: операция просто остаётся 'running'."""
        started.append(int(row["id"]))

    rnd = random.Random(20261022)
    # Сид фиксирован: шторм обязан быть ВОСПРОИЗВОДИМЫМ, иначе красный прогон
    # не повторить, а значит и не разобрать.

    seen_statuses: set = set()

    async def _age_running():
        await pool.execute(
            "UPDATE operation_queue SET started_at = now() - "
            "make_interval(mins => $2) WHERE owner_id=$1 AND status='running'",
            OWNER, ow._STALE_RUNNING_TIMEOUT_MIN + 5)

    async def _forget_active():
        """Падение процесса: активные забыты, строки остались 'running'."""
        async with ow._active_lock:
            ow._active_op_ids.clear()
        ow._shutting_down = False

    async def _drive_budget_to_the_end():
        """Довести операции до гашения по бюджету — руками, а не случаем.

        Случайный шторм до 'failed' доходит не на всяком сиде, и тогда самый
        дорогой путь (операция ВЕШАЕТ воркер и обязана быть погашена, а не
        крутиться вечно) оставался непроверенным при зелёном тесте.
        """
        for _ in range(ow._MAX_REVIVES + 1):
            await ow._process_pending(pool, None)
            await _age_running()
            await _forget_active()
            await ow._reset_stale_running(pool)
            bad = await _violations(pool)
            assert not bad, "на пути к гашению по бюджету: " + "; ".join(bad[:4])
            for _r in await pool.fetch(
                    "SELECT DISTINCT status FROM operation_queue WHERE owner_id=$1",
                    OWNER):
                seen_statuses.add(_r["status"])

    async def _storm():
        await _drive_budget_to_the_end()
        for step in range(60):
            what = rnd.choice(("pick", "pick", "crash", "shutdown", "startup",
                               "watchdog", "age"))
            if what == "pick":
                await ow._process_pending(pool, None)
            elif what == "crash":
                await _forget_active()
            elif what == "shutdown":
                await ow.shutdown(pool, grace_s=0.0)
                ow._shutting_down = False
            elif what == "startup":
                await ow._reset_stale_running(pool)
            elif what == "watchdog":
                await ow._watchdog_stale(pool, None)
            elif what == "age":
                await _age_running()
            bad = await _violations(pool)
            assert not bad, f"шаг {step} ({what}): " + "; ".join(bad[:4])
            for _r in await pool.fetch(
                    "SELECT DISTINCT status FROM operation_queue WHERE owner_id=$1",
                    OWNER):
                seen_statuses.add(_r["status"])

    _orig = ow._run_op_task_guarded
    ow._run_op_task_guarded = _fake_task
    try:
        _run(_storm())
    finally:
        ow._run_op_task_guarded = _orig
        # За собой убираем и память модуля: следующий тест в этом же процессе
        # иначе получит чужие активные операции и флаг остановки.
        _run(_forget_active_state(ow))
        _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))

    # Шторм обязан побывать во всех трёх состояниях, иначе инвариант нечему
    # нарушать и тест зелёный просто от бездействия.
    assert {"pending", "running", "failed"} <= seen_statuses, (
        f"шторм не дошёл до интересных состояний: {sorted(seen_statuses)}")
    assert started, "поллер не взял ни одной операции за весь шторм"


def test_the_storm_actually_moved_the_queue(pool):
    """Самопроверка: шторм обязан реально двигать операции, а не стоять на месте.

    Без этого предыдущий тест был бы зелёным и на сломанной очереди: если
    подхват ничего не берёт, инвариант нечему нарушать.
    """
    from services import op_worker as ow

    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(_seed(pool, 3))
    seen: list[int] = []

    async def _fake_task(_pool, _bot, row):
        seen.append(int(row["id"]))

    _orig = ow._run_op_task_guarded
    ow._run_op_task_guarded = _fake_task
    try:
        _run(ow._process_pending(pool, None))
    finally:
        ow._run_op_task_guarded = _orig

    assert seen, "поллер не взял ни одной операции — шторм выше ничего не проверяет"
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))


# ── самопроверка измерителя: он обязан видеть то, что ищет ──────────────────

def test_the_detector_sees_each_violation(pool):
    """Здоровая строка — молчит, каждая сломанная — находится.

    Без этой проверки зелёный шторм выше не значит ничего: измеритель,
    который ничего не умеет найти, зелёный всегда.
    """
    from services import op_worker as ow

    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))

    async def _row(**over):
        base = dict(status="pending", started=None, done=1, total=10, revives=0)
        base.update(over)
        return await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, params, status, "
            "started_at, done_items, total_items, revive_count) "
            "VALUES($1,'mass_publish','{}'::jsonb,$2,$3,$4,$5,$6) RETURNING id",
            OWNER, base["status"], base["started"], base["done"], base["total"],
            base["revives"])

    # Здоровая.
    _run(_row())
    assert _run(_violations(pool)) == [], "измеритель ругается на здоровую строку"

    import datetime as _dt

    _now = _dt.datetime.now(_dt.timezone.utc)
    cases = {
        "done_items": dict(done=34, total=17),
        "в 'pending', но со started_at": dict(status="pending", started=_now),
        "в 'running' без started_at": dict(status="running", started=None),
        "превысил": dict(status="running", started=_now, revives=ow._MAX_REVIVES + 5),
    }
    for needle, over in cases.items():
        _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
        _run(_row(**over))
        found = _run(_violations(pool))
        assert any(needle in f for f in found), (
            f"измеритель не увидел нарушение {over}: {found}")

    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
