"""Уборка журнала целей не превращает повтор в дубль реальных действий.

РАЗРЫВ. Идемпотентность повтора стоит на `operation_log`:
`op_worker.completed_targets` и `completed_steps` считают сделанным ровно то,
что есть в журнале. Уборка (`services/db_maintenance`) чистила журнал по одному
ВОЗРАСТУ — `created_at < NOW() - 30 days`, без оглядки на статус операции.

Недоведённая операция (`failed`/`partial`) остаётся в списке «повторить» у
владельца навсегда: её статус не меняется от того, что время прошло. Нажатие
через месяц означало повтор с пустым журналом, то есть ВСЮ работу заново — те
же приглашения тем же людям (риск бана), второй пост в канал, второе сообщение
человеку. Молча: ни отказа, ни предупреждения.

Тот же разрыв уже был закрыт у рассылок (`broadcasts.delivery_log_pruned`):
сначала отметка, потом удаление строк, а повтор по помеченной рассылке
отказывает. Здесь проверяется то же решение для операций.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяются СЕМАНТИКА запросов уборки (какие строки
попадают под условие) и порядок двух запросов. Заглушка пула не выполняет SQL
вовсе, то есть «журнал уцелел» на ней не проверяется в принципе. Рецепт
запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
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

OWNER = 991777
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


async def _op(pool, status: str, *, age_days: int, params: dict | None = None) -> int:
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, total_items, "
        "done_items) VALUES($1,'mass_invite',$2::jsonb,$3,10,4) RETURNING id",
        OWNER, json.dumps(params or {}), status)
    await pool.execute(
        "UPDATE operation_queue SET created_at = NOW() - make_interval(days => $2), "
        "finished_at = CASE WHEN $3 THEN NOW() - make_interval(days => $2) END "
        " WHERE id = $1",
        op_id, int(age_days), status not in ("pending", "running"))
    await pool.execute(
        "INSERT INTO operation_log(op_id, step_num, target, status, message) "
        "VALUES($1, 1, 'uid#1', 'ok', 'приглашён')", op_id)
    await pool.execute(
        "UPDATE operation_log SET created_at = NOW() - make_interval(days => $2) "
        " WHERE op_id = $1", op_id, int(age_days))
    return op_id


async def _journal_rows(pool, op_id: int) -> int:
    return int(await pool.fetchval(
        "SELECT COUNT(*) FROM operation_log WHERE op_id=$1", op_id) or 0)


def test_journal_of_a_running_operation_is_never_pruned(pool):
    """По нему идёт возобновление прямо сейчас — возраст тут ничего не значит."""
    from services import db_maintenance

    op_id = _run(_op(pool, "running", age_days=400))
    _run(db_maintenance._prune_operation_log(pool))
    assert _run(_journal_rows(pool, op_id)) == 1, (
        "у идущей операции убрали журнал — она пойдёт делать сделанное заново")
    assert _run(pool.fetchval(
        "SELECT journal_pruned FROM operation_queue WHERE id=$1", op_id)) is False


def test_a_fresh_unfinished_operation_keeps_its_journal(pool):
    """Свежую недоведённую владелец повторяет кнопкой — журнал обязан быть цел."""
    from services import db_maintenance

    op_id = _run(_op(pool, "partial", age_days=3))
    _run(db_maintenance._prune_operation_log(pool))
    assert _run(_journal_rows(pool, op_id)) == 1


def test_an_old_finished_operation_is_marked_before_its_journal_goes(pool):
    """Порядок: сначала отметка, потом удаление. Обратный порядок — это дубли."""
    from services import db_maintenance

    op_id = _run(_op(pool, "done", age_days=90))
    deleted, err = _run(db_maintenance._prune_operation_log(pool))
    assert err is None, err
    assert deleted >= 1
    assert _run(_journal_rows(pool, op_id)) == 0
    assert _run(pool.fetchval(
        "SELECT journal_pruned FROM operation_queue WHERE id=$1", op_id)) is True


def test_deleting_the_operation_takes_its_journal_with_it(pool):
    """Почему в уборщике нет прохода по сиротам: их не бывает.

    `operation_log.op_id` ссылается на `operation_queue` с ON DELETE CASCADE.
    Проход по сиротам был бы мёртвым кодом, а мёртвый код в уборке опаснее
    отсутствующего: его никто не проверяет, а выглядит он как защита.
    """
    op_id = _run(_op(pool, "done", age_days=1))
    _run(pool.execute("DELETE FROM operation_queue WHERE id=$1", op_id))
    assert _run(_journal_rows(pool, op_id)) == 0


def test_retry_refuses_when_the_journal_is_gone(pool):
    """Иначе повтор молча делает все цели второй раз."""
    from services import operation_bus

    op_id = _run(_op(pool, "partial", age_days=90))
    _run(db_maintenance_prune(pool))
    res = _run(operation_bus.resubmit_one(pool, OWNER, op_id))
    assert res["ok"] is False
    assert res["code"] == 409
    assert "журнал" in res["reason"].lower(), res["reason"]
    assert "заново" in res["reason"].lower(), (
        "отказ обязан объяснять владельцу, почему нельзя, а не просто запрещать")


def test_retry_refuses_when_the_ANCESTOR_journal_is_gone(pool):
    """У свежего повтора журнал свой, а идемпотентность он берёт у предка."""
    from services import operation_bus

    ancestor = _run(_op(pool, "partial", age_days=90))
    _run(db_maintenance_prune(pool))
    child = _run(_op(pool, "partial", age_days=1,
                     params={"retry_of_op": int(ancestor)}))
    res = _run(operation_bus.resubmit_one(pool, OWNER, child))
    assert res["ok"] is False and res["code"] == 409, res
    assert "журнал" in res["reason"].lower()


def test_bulk_retry_does_not_offer_a_pruned_operation(pool):
    """Массовый повтор тоже обязан их не трогать."""
    from services import operation_bus

    op_id = _run(_op(pool, "partial", age_days=0))
    _run(pool.execute(
        "UPDATE operation_queue SET journal_pruned = TRUE WHERE id=$1", op_id))
    res = _run(operation_bus.resubmit_unfinished(pool, OWNER, hours=48))
    assert res["retried"] == 0, (
        "операция с убранным журналом попала в массовый повтор — это дубли")


def db_maintenance_prune(pool):
    from services import db_maintenance

    return db_maintenance._prune_operation_log(pool)
