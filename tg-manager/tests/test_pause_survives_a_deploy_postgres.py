"""Пауза владельца переживает рестарт воркера, а не отпускается на нём.

РАЗРЫВ. «Пауза» переводит очередь `pending → paused`, а уже ИДУЩУЮ операцию
сознательно не трогает: прервать её на полпути без чекпоинта означало бы
пере-прогон и дубли реальных действий — повторные приглашения тем же людям,
второй пост, второе сообщение человеку. Это верно, пока проход идёт.

Но проход заканчивается САМ, и довольно скоро: ветка едет на Railway, то есть
SIGTERM на каждом деплое; есть сброс зависшей операции и отсрочка по
флуд-паузе. Все эти пути возвращают операцию в очередь со `status='pending'` —
и поллер запускал её ЗАНОВО. То есть аварийный тормоз продукта отпускался
ровно в тот момент, когда операцию наконец можно было остановить безопасно, и
владелец, нажавший паузу из-за банов или неверного текста, получал новый
прогон.

Намерение теперь хранится отдельно от статуса (`pause_requested`,
schema_v249): статус говорит, где операция сейчас, флаг — что делать на
следующем входе в работу. Снимает его только «Старт».

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяется семантика ДВУХ запросов поллера (перевод
помеченных в 'paused' и условие отбора) и то, что ручки мини-аппа пишут и
снимают флаг по тем же строкам. Заглушка пула SQL не выполняет вовсе. Рецепт
запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest
from aiohttp import web

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991784
OTHER_OWNER = 991785
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
    from services import op_worker

    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])",
                      [OWNER, OTHER_OWNER]))
    op_worker._active_op_ids.clear()
    op_worker._shutting_down = False


async def _op(pool, *, status: str = "running", paused: bool = False) -> int:
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items, "
        "done_items, pause_requested) "
        "VALUES($1,'mass_invite',$2::jsonb,$3,10,4,$4) RETURNING id",
        OWNER, json.dumps({}), status, paused)
    if status == "running":
        await pool.execute(
            "UPDATE operation_queue SET started_at = now() WHERE id=$1", op_id)
    return op_id


async def _status(pool, op_id: int) -> str:
    return await pool.fetchval(
        "SELECT status FROM operation_queue WHERE id=$1", op_id)


async def _flag(pool, op_id: int) -> bool:
    return await pool.fetchval(
        "SELECT pause_requested FROM operation_queue WHERE id=$1", op_id)


@pytest.fixture
def started(monkeypatch):
    """Перехват запуска: поллер не должен поднимать исполнителя по-настоящему."""
    from services import op_worker

    seen: list[int] = []

    async def _fake(pool, bot, row):
        seen.append(int(row["id"]))

    monkeypatch.setattr(op_worker, "_run_op_task_guarded", _fake)
    return seen


# ── Главное: пауза доживает до следующего входа в работу ─────────────────────

def test_a_paused_operation_is_not_restarted_after_a_deploy(pool, started):
    """Деплой возвращает идущую операцию в очередь — и она обязана встать."""
    from services import op_worker

    op_id = _run(_op(pool, status="running", paused=True))
    _run(op_worker._reset_stale_running(pool))      # то, что делает старт воркера
    assert _run(_status(pool, op_id)) == "pending", "сброс зависшей не сработал"

    _run(op_worker._process_pending(pool, None))

    assert op_id not in started, (
        "операция запустилась ЗАНОВО, хотя владелец нажал паузу — тормоз "
        "отпустился ровно на рестарте")
    assert _run(_status(pool, op_id)) == "paused", (
        "операция осталась в 'pending': владелец видит «ожидает» и не понимает, "
        "сработала пауза или нет")


def test_an_operation_without_the_flag_is_still_picked_up(pool, started):
    """Самопроверка: без паузы поллер работает как раньше."""
    from services import op_worker

    op_id = _run(_op(pool, status="running", paused=False))
    _run(op_worker._reset_stale_running(pool))
    _run(op_worker._process_pending(pool, None))

    assert op_id in started, "обычная операция перестала подхватываться"
    assert _run(_status(pool, op_id)) == "running"


def test_the_pause_is_not_consumed_by_one_tick(pool, started):
    """Флаг — намерение, а не одноразовый пропуск: снимает его только «Старт»."""
    from services import op_worker

    op_id = _run(_op(pool, status="pending", paused=True))
    _run(op_worker._process_pending(pool, None))
    _run(pool.execute("UPDATE operation_queue SET status='pending' WHERE id=$1", op_id))
    _run(op_worker._process_pending(pool, None))

    assert op_id not in started
    assert _run(_flag(pool, op_id)) is True, "намерение исчезло само собой"


def test_another_owners_operation_is_untouched(pool, started):
    """Пауза одного владельца не смеет останавливать работу другого."""
    from services import op_worker

    mine = _run(_op(pool, status="pending", paused=True))
    other = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items, "
        "done_items) VALUES($1,'mass_invite','{}'::jsonb,'pending',5,0) RETURNING id",
        OTHER_OWNER))

    _run(op_worker._process_pending(pool, None))

    assert other in started, "операция соседа встала из-за чужой паузы"
    assert mine not in started


# ── Ручки мини-аппа: пишут и снимают намерение по тем же строкам ─────────────

def _handler(pool, method: str, path: str):
    from services import mini_app_api

    app = web.Application()
    mini_app_api.setup_routes(app, pool)
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        if route.method == method and (info.get("path") or info.get("formatter")) == path:
            return route.handler
    raise AssertionError(f"роут не найден: {method} {path}")


class _Req:
    def __init__(self, match_info=None):
        self.match_info = match_info or {}
        self.query: dict[str, str] = {}
        self.headers: dict[str, str] = {}

    async def json(self):
        return {}


@pytest.fixture
def as_owner(monkeypatch):
    from services import mini_app_api

    monkeypatch.setattr(mini_app_api, "_get_uid", lambda request: OWNER)


def test_pausing_the_queue_marks_the_running_operation(pool, as_owner):
    """Иначе «Пауза» для идущей операции не значит ничего вообще."""
    op_id = _run(_op(pool, status="running"))
    handler = _handler(pool, "POST", "/api/miniapp/operations/pause")

    resp = _run(handler(_Req()))

    assert resp.status == 200, resp.body
    assert _run(_status(pool, op_id)) == "running", (
        "идущую операцию рвать нельзя: пере-прогон продублировал бы действия")
    assert _run(_flag(pool, op_id)) is True, "намерение не записано"


def test_resuming_the_queue_lets_the_work_go_on(pool, as_owner, started):
    """«Старт» после паузы обязан отпускать тормоз полностью."""
    from services import op_worker

    op_id = _run(_op(pool, status="running", paused=True))
    _run(op_worker._reset_stale_running(pool))
    _run(op_worker._process_pending(pool, None))
    assert _run(_status(pool, op_id)) == "paused"

    resume = _handler(pool, "POST", "/api/miniapp/operations/resume")
    resp = _run(resume(_Req()))
    assert resp.status == 200, resp.body
    assert _run(_flag(pool, op_id)) is False, (
        "флаг остался: операция встала бы на паузу снова на следующем тике")

    _run(op_worker._process_pending(pool, None))
    assert op_id in started, "возобновлённая операция так и не пошла в работу"


def test_resuming_one_running_operation_cancels_the_pause(pool, as_owner):
    """Владелец передумал, пока проход ещё шёл."""
    op_id = _run(_op(pool, status="running", paused=True))
    handler = _handler(pool, "POST", "/api/miniapp/operation/{op_id}/resume")

    resp = _run(handler(_Req({"op_id": str(op_id)})))

    assert resp.status == 200, resp.body
    assert _run(_flag(pool, op_id)) is False
    assert _run(_status(pool, op_id)) == "running"
