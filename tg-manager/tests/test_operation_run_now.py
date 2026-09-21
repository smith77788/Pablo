"""«Запустить сейчас» для отложенной операции + честный resume.

Первопричина. Живая очередь (_requeue_op_no_accounts, см. соседний фикс
"fix(fleet): занятый флот проваливал операцию вместо живой очереди") при
занятости флота выставляет `operation_queue.scheduled_for` в будущее и ждёт.
Экран деталей операции показывал это будущее время («⏰ Запланировано»), но
единственные кнопки — «Приостановить»/«Отменить»: способа поторопить
операцию, не дожидаясь исходного расписания, не было вовсе.

Заодно: `resume_operation` (paused → pending) не снимал scheduled_for —
пауза операции, которую живая очередь уже отложила на будущее, а затем
«возобновление» молча досиживало остаток СТАРОГО расписания вместо
немедленного нового шанса, хотя кнопка называется «Возобновить», а не
«Продолжить ждать».

Реальный Postgres — нужно настоящее UPDATE ... WHERE owner_id=$2 AND
status=... RETURNING id и то, что op_worker'овский критерий подбора кандидатов
(scheduled_for IS NULL OR scheduled_for <= now()) после этого действительно
видит операцию как готовую.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

from tests.miniapp_source import miniapp_source

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark_pg = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991701
OTHER_OWNER = 991702

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── статика: маршрут, обработчик, фронтенд ───────────────────────────────────

def test_run_now_route_registered():
    src = _read("services/mini_app_api.py")
    assert 'app.router.add_post("/api/miniapp/operation/{op_id}/run_now", run_now_operation)' in src


def test_run_now_handler_scoped_and_pending_only():
    src = _read("services/mini_app_api.py")
    i = src.index("async def run_now_operation")
    seg = src[i:src.index("\n    async def ", i + 10)]
    assert "SET scheduled_for=NULL" in seg
    assert "status='pending'" in seg
    assert "owner_id=$2" in seg


def test_resume_operation_also_clears_scheduled_for():
    src = _read("services/mini_app_api.py")
    i = src.index("async def resume_operation")
    seg = src[i:src.index("\n    async def ", i + 10)]
    assert "scheduled_for=NULL" in seg, (
        "возобновление приостановленной операции обязано снимать старое "
        "расписание — иначе 'Возобновить' молча ждёт исходное время"
    )


def test_ui_has_run_now_button_only_for_scheduled_pending():
    ui = miniapp_source()
    assert "runNowOp" in ui
    i = ui.index("async function runNowOp")
    seg = ui[i:ui.index("\n}", i)]
    assert "/run_now" in seg

    # Кнопка условна на реально будущем scheduled_for — не на любом pending.
    j = ui.index("if (o.status==='pending')")
    block = ui[j:j + 700]
    assert "runNowOp(" in block
    assert "_scheduled" in block


# ── реальный Postgres: DB-эффект и подбор кандидатов op_worker ──────────────

@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id IN ($1,$2)", OWNER, OTHER_OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id IN ($1,$2)", OWNER, OTHER_OWNER))
    yield


async def _mk_scheduled_op(pool, owner_id: int, seconds_ahead: int = 300) -> int:
    return await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, "
        "label, scheduled_for) "
        "VALUES($1,'mass_invite','pending','{}',1,'run-now test', "
        "now() + make_interval(secs => $2)) RETURNING id",
        owner_id, seconds_ahead,
    )


def test_run_now_makes_op_worker_pick_the_operation_up_immediately(pool):
    op_id = _run(_mk_scheduled_op(pool, OWNER))

    is_candidate_before = _run(pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue "
        "WHERE id=$1 AND status='pending' AND (scheduled_for IS NULL OR scheduled_for <= now())",
        op_id))
    assert is_candidate_before == 0, "тест бессмысленнен, если операция и так уже готова"

    # Тот же UPDATE, что делает run_now_operation.
    row = _run(pool.fetchrow(
        "UPDATE operation_queue SET scheduled_for=NULL "
        "WHERE id=$1 AND owner_id=$2 AND status='pending' RETURNING id",
        op_id, OWNER))
    assert row is not None

    is_candidate_after = _run(pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue "
        "WHERE id=$1 AND status='pending' AND (scheduled_for IS NULL OR scheduled_for <= now())",
        op_id))
    assert is_candidate_after == 1, "после run_now операция обязана стать кандидатом для воркера"


def test_run_now_is_owner_scoped(pool):
    op_id = _run(_mk_scheduled_op(pool, OWNER))

    row = _run(pool.fetchrow(
        "UPDATE operation_queue SET scheduled_for=NULL "
        "WHERE id=$1 AND owner_id=$2 AND status='pending' RETURNING id",
        op_id, OTHER_OWNER))
    assert row is None, "чужой владелец не должен мочь поторопить чужую операцию"

    still_scheduled = _run(pool.fetchval(
        "SELECT scheduled_for IS NOT NULL FROM operation_queue WHERE id=$1", op_id))
    assert still_scheduled is True


def test_run_now_does_not_touch_running_or_paused(pool):
    op_id = _run(_mk_scheduled_op(pool, OWNER))
    _run(pool.execute("UPDATE operation_queue SET status='running' WHERE id=$1", op_id))

    row = _run(pool.fetchrow(
        "UPDATE operation_queue SET scheduled_for=NULL "
        "WHERE id=$1 AND owner_id=$2 AND status='pending' RETURNING id",
        op_id, OWNER))
    assert row is None, "запущенную операцию 'запустить сейчас' не должно трогать"


def test_resume_clears_stale_schedule_for_real(pool):
    """Сквозной сценарий бага: операцию отложила живая очередь (scheduled_for в
    будущем), её поставили на паузу, затем возобновили — обязана стать
    кандидатом СРАЗУ, а не через остаток старого расписания."""
    op_id = _run(_mk_scheduled_op(pool, OWNER, seconds_ahead=600))
    _run(pool.execute(
        "UPDATE operation_queue SET status='paused' WHERE id=$1 AND status='pending'", op_id))

    # Тот же UPDATE, что делает resume_operation после фикса.
    row = _run(pool.fetchrow(
        "UPDATE operation_queue SET status='pending', scheduled_for=NULL "
        "WHERE id=$1 AND owner_id=$2 AND status='paused' RETURNING id",
        op_id, OWNER))
    assert row is not None

    is_candidate = _run(pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue "
        "WHERE id=$1 AND status='pending' AND (scheduled_for IS NULL OR scheduled_for <= now())",
        op_id))
    assert is_candidate == 1, "возобновлённая операция обязана быть кандидатом немедленно"
