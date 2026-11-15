"""Горизонтальный исполнитель: операции разбирают несколько процессов, не мешая друг другу.

ПОТОЛОК, КОТОРЫЙ СНИМАЕТСЯ. Очередь разбирал один процесс — держатель аренды
исполнителя. Все операции всех клиентов, все клиенты Telethon и ~70 фоновых
циклов жили в одном цикле событий одного ядра, и добавить мощности было
нечем: вторая реплика воркера только ждала аренду. Мешали сторожа: и уборка
на старте, и сторож зависших возвращали в очередь ВСЁ 'running', то есть
живые операции соседа.

С OP_SHARDED_EXECUTOR=1 у запущенной операции есть хозяин
(`operation_queue.claimed_by`), и проверяется здесь на живой базе:
  * отбор пишет хозяина;
  * уборка на старте лидера и сторож зависших не трогают живые операции
    соседа, но чинят бесхозные и операции УМЕРШИХ процессов;
  * операции умершего процесса возвращаются сразу, а не через час;
  * процесс, у которого операцию забрали, гасит свою задачу (второго прогона
    нет);
  * два одновременных отбора не превышают лимит на владельца.

Заглушка пула здесь бесполезна: всё держится на тексте SQL и на том, что
Postgres его примет (параметры, подзапрос к process_heartbeats).
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

OWNER = 991_846
LIVE = "test-live-worker:1:aaaa"
DEAD = "test-dead-worker:2:bbbb"
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
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=2, max_size=6)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool, monkeypatch):
    from services import op_worker

    monkeypatch.setenv("OP_SHARDED_EXECUTOR", "1")
    # Поллер глобальный: чужие pending/running тестовых владельцев из других
    # файлов набора вытесняют свои строки из окна отбора.
    _run(pool.execute(
        "DELETE FROM operation_queue WHERE owner_id >= 990000 "
        "AND status IN ('pending','running')"))
    _run(pool.execute(
        "DELETE FROM process_heartbeats WHERE worker_id = ANY($1::text[])",
        [LIVE, DEAD, op_worker._WORKER_ID]))
    # Живой сосед бьётся сейчас, умерший — молчит дольше окна.
    _run(pool.execute(
        "INSERT INTO process_heartbeats(worker_id, role, started_at, last_seen) "
        "VALUES ($1,'worker',now(),now()), "
        "       ($2,'worker',now()-interval '2 hours',now()-interval '1 hour')",
        LIVE, DEAD))
    op_worker._active_op_ids.clear()
    op_worker._shutting_down = False
    yield
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id = $1", OWNER))
    _run(pool.execute(
        "DELETE FROM process_heartbeats WHERE worker_id = ANY($1::text[])",
        [LIVE, DEAD]))


async def _op(pool, *, status="running", claimed_by=None, started_min_ago=0) -> int:
    return await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items,"
        " done_items, claimed_by, started_at, created_at) "
        "VALUES($1,'mass_invite',$2::jsonb,$3,10,4,$4,"
        " CASE WHEN $3='running' THEN now() - make_interval(mins => $5) END,"
        " now() - make_interval(mins => $5 + 1)) RETURNING id",
        OWNER, json.dumps({}), status, claimed_by, started_min_ago)


async def _row(pool, op_id):
    return await pool.fetchrow(
        "SELECT status, claimed_by, last_error FROM operation_queue WHERE id=$1", op_id)


@pytest.fixture
def started(monkeypatch):
    from services import op_worker

    seen: list[int] = []

    async def _fake(pool, bot, row):
        seen.append(int(row["id"]))

    monkeypatch.setattr(op_worker, "_run_op_task_guarded", _fake)
    return seen


def test_claim_records_the_owner_process(pool, started):
    from services import op_worker

    op_id = _run(_op(pool, status="pending"))
    _run(op_worker._process_pending(pool, None))
    assert op_id in started
    r = _run(_row(pool, op_id))
    assert r["status"] == "running"
    assert r["claimed_by"] == op_worker._WORKER_ID


def test_leader_startup_cleanup_spares_live_neighbours(pool):
    from services import op_worker

    live = _run(_op(pool, claimed_by=LIVE))
    dead = _run(_op(pool, claimed_by=DEAD))
    legacy = _run(_op(pool, claimed_by=None))
    mine = _run(_op(pool, claimed_by=op_worker._WORKER_ID))
    _run(op_worker._reset_stale_running(pool, None))

    assert _run(_row(pool, live))["status"] == "running", (
        "уборка лидера вернула в очередь ЖИВУЮ операцию соседа — второй прогон "
        "тех же реальных действий")
    assert _run(_row(pool, mine))["status"] == "running", (
        "уборка сбросила собственную идущую операцию процесса")
    assert _run(_row(pool, dead))["status"] == "pending"
    assert _run(_row(pool, legacy))["status"] == "pending"


def test_single_executor_cleanup_is_unchanged(pool, monkeypatch):
    """Без горизонтального режима уборка на старте — как раньше: всё 'running'."""
    from services import op_worker

    monkeypatch.delenv("OP_SHARDED_EXECUTOR")
    live = _run(_op(pool, claimed_by=LIVE))
    _run(op_worker._reset_stale_running(pool, None))
    assert _run(_row(pool, live))["status"] == "pending"


def test_stale_watchdog_spares_live_neighbours(pool):
    from services import op_worker

    live = _run(_op(pool, claimed_by=LIVE, started_min_ago=120))
    dead = _run(_op(pool, claimed_by=DEAD, started_min_ago=120))
    orphan = _run(_op(pool, claimed_by=op_worker._WORKER_ID, started_min_ago=120))
    _run(op_worker._watchdog_stale(pool, None))
    assert _run(_row(pool, live))["status"] == "running"
    assert _run(_row(pool, dead))["status"] == "pending"
    # своя 'running' без живой задачи в процессе — настоящая сирота
    assert _run(_row(pool, orphan))["status"] == "pending"


def test_follower_watchdog_touches_only_its_own(pool):
    from services import op_worker

    live = _run(_op(pool, claimed_by=LIVE, started_min_ago=120))
    dead = _run(_op(pool, claimed_by=DEAD, started_min_ago=120))
    orphan = _run(_op(pool, claimed_by=op_worker._WORKER_ID, started_min_ago=120))
    _run(op_worker._watchdog_stale_mine(pool, None))
    assert _run(_row(pool, live))["status"] == "running"
    assert _run(_row(pool, dead))["status"] == "running", (
        "чужих умерших чинит лидер, а не каждый исполнитель")
    assert _run(_row(pool, orphan))["status"] == "pending"


def test_dead_executor_ops_come_back_at_once(pool):
    from services import op_worker

    dead = _run(_op(pool, claimed_by=DEAD, started_min_ago=1))
    live = _run(_op(pool, claimed_by=LIVE, started_min_ago=1))
    mine = _run(_op(pool, claimed_by=op_worker._WORKER_ID, started_min_ago=1))
    _run(op_worker._sweep_dead_executor_ops(pool, None))
    r = _run(_row(pool, dead))
    assert r["status"] == "pending", (
        "операция умершего исполнителя ждала бы общего сторожа (60 мин)")
    assert "Процесс, который выполнял операцию, остановился" in (r["last_error"] or "")
    assert _run(_row(pool, live))["status"] == "running"
    assert _run(_row(pool, mine))["status"] == "running"


def test_process_fences_an_op_taken_by_someone_else(pool):
    from services import op_worker

    async def go():
        op_id = await _op(pool, claimed_by=LIVE)    # уже у другого процесса
        hold = asyncio.Event()

        async def _body():
            await hold.wait()

        t = asyncio.create_task(_body(), name=f"op:{op_id}")
        async with op_worker._active_lock:
            op_worker._active_op_ids.add(op_id)
            op_worker._active_op_tasks.add(t)
        try:
            lost = await op_worker._fence_lost_ops(pool)
            await asyncio.sleep(0)
            return op_id, lost, t.cancelled() or t.done()
        finally:
            async with op_worker._active_lock:
                op_worker._active_op_ids.discard(op_id)
                op_worker._active_op_tasks.discard(t)

    op_id, lost, stopped = _run(go())
    assert lost == [op_id]
    assert stopped, "задача операции, которую выполняет другой процесс, продолжала работать"


def test_own_running_op_is_not_fenced(pool):
    from services import op_worker

    async def go():
        op_id = await _op(pool, claimed_by=op_worker._WORKER_ID)
        async with op_worker._active_lock:
            op_worker._active_op_ids.add(op_id)
        try:
            return await op_worker._fence_lost_ops(pool)
        finally:
            async with op_worker._active_lock:
                op_worker._active_op_ids.discard(op_id)

    assert _run(go()) == []


def test_concurrent_claims_keep_the_per_owner_limit(pool, started, monkeypatch):
    from services import op_worker

    monkeypatch.setattr(op_worker, "_MAX_PARALLEL", 50)
    for _ in range(12):
        _run(_op(pool, status="pending"))

    async def go():
        await asyncio.gather(*(op_worker._process_pending(pool, None) for _ in range(4)))

    _run(go())
    n = _run(pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='running'",
        OWNER))
    assert n == op_worker._MAX_PARALLEL_PER_OWNER, (
        f"одновременные отборы взяли {n} операций одного владельца при лимите "
        f"{op_worker._MAX_PARALLEL_PER_OWNER}")
