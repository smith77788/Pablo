"""Второй проход «каналов ноды» на ЖИВОЙ базе не создаёт ни одного топика.

Юнит-тест рядом (`test_a_community_channel_is_not_created_twice.py`) подменяет
`completed_steps` и проверяет решение исполнителя. Он не проверяет главного:
что писатель отметки и её читатель договорились между собой. Отметку пишет
`_journal_write` в `operation_log(op_id, step_num, …, status='ok')`, а читает
её `completed_steps` своим запросом через `journal_op_ids` — и если бы эти два
запроса расходились хоть в одном поле, дверь выглядела бы закрытой, а повтор
всё равно создавал бы второй комплект топиков. Такое расхождение заглушкой
пула не поймать: нужен настоящий Postgres.

Поэтому здесь исполнитель гоняется дважды по одному op_id, а Телеграм
подменён — в журнал пишет и из журнала читает настоящий код.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993442
NODE = 51
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


async def _clean(p) -> None:
    await p.execute(
        "DELETE FROM operation_log WHERE op_id IN "
        "(SELECT id FROM operation_queue WHERE owner_id=$1)", OWNER)
    await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
    await p.execute("DELETE FROM platform_users WHERE user_id=$1", OWNER)


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
    _run(_clean(p))
    _run(p.execute(
        "INSERT INTO platform_users(user_id, username) VALUES($1,'node_ch_test') "
        "ON CONFLICT (user_id) DO NOTHING", OWNER))
    yield p
    _run(_clean(p))
    _run(p.close())


@pytest.fixture
def created(monkeypatch):
    """Телеграма нет: фабрика только запоминает имена и выдаёт id топика."""
    from services import nodes_engine

    names: list[str] = []

    async def _create(pool, bot, owner_id, node_id, name):
        names.append(str(name))
        return {"id": len(names), "node_id": node_id, "name": str(name),
                "tg_topic_id": 1000 + len(names)}

    monkeypatch.setattr(nodes_engine, "create_community_channel", _create)
    return names


def _op(pool, n: int) -> int:
    async def _mk():
        return int(await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, "
            "   total_items, label, started_at) "
            "VALUES($1,'community_add_channel','running','{}'::jsonb,$2,"
            "       'каналы ноды',now()) RETURNING id", OWNER, n))
    return _run(_mk())


def _pass(pool, op_id, names):
    from services import op_worker as ow
    return _run(ow._exec_community_add_channel(
        pool, object(), op_id, OWNER, {"node_id": NODE, "names": list(names)}))


NAMES = ["Новости", "Чат", "Барахолка"]


def test_the_second_pass_creates_nothing_and_still_counts_all(pool, created):
    """Первый проход создаёт три топика, второй — ни одного."""
    op_id = _op(pool, len(NAMES))

    first = _pass(pool, op_id, NAMES)
    assert created == NAMES, created
    assert first["created"] == 3, first

    marks = _run(pool.fetch(
        "SELECT step_num, target, status FROM operation_log WHERE op_id=$1 "
        "ORDER BY step_num", op_id))
    assert [(r["step_num"], r["target"], r["status"]) for r in marks] == [
        (1, "Новости", "ok"), (2, "Чат", "ok"), (3, "Барахолка", "ok")], marks

    second = _pass(pool, op_id, NAMES)
    assert created == NAMES, (
        f"повтор создал топики заново: {created[len(NAMES):]}")
    assert second["created"] == 3, (
        "повтор обязан отчитаться обо всех каналах ноды, а не о нуле")
    assert second["status"] == "done", second


def test_the_reader_sees_exactly_what_the_writer_wrote(pool, created):
    """Договор писателя и читателя: номера шагов совпадают один в один.

    Это и есть то, чего не видно на заглушке пула: `completed_steps` идёт в
    базу своим запросом, а не по тому же объекту, что писал отметку.
    """
    from services import op_worker as ow

    op_id = _op(pool, len(NAMES))
    _pass(pool, op_id, NAMES)
    assert _run(ow.completed_steps(pool, op_id)) == {1, 2, 3}


def test_a_half_done_pass_finishes_the_rest(pool, created, monkeypatch):
    """Прогон упал на втором имени — повтор делает ровно недоделанное."""
    from services import nodes_engine

    async def _create_then_fail(pool_, bot, owner_id, node_id, name):
        created.append(str(name))
        if len(created) >= 2:
            raise RuntimeError("Телеграм ответил ошибкой")
        return {"id": 1, "node_id": node_id, "name": str(name),
                "tg_topic_id": 2001}

    monkeypatch.setattr(nodes_engine, "create_community_channel", _create_then_fail)
    op_id = _op(pool, len(NAMES))
    with pytest.raises(RuntimeError):
        _pass(pool, op_id, NAMES)
    assert created == ["Новости", "Чат"], created

    monkeypatch.undo()
    rest: list[str] = []

    async def _create_ok(pool_, bot, owner_id, node_id, name):
        rest.append(str(name))
        return {"id": 9, "node_id": node_id, "name": str(name),
                "tg_topic_id": 3000 + len(rest)}

    monkeypatch.setattr(nodes_engine, "create_community_channel", _create_ok)
    res = _pass(pool, op_id, NAMES)
    assert rest == ["Чат", "Барахолка"], (
        "повтор обязан начать со второго имени: первое уже отмечено")
    assert res["created"] == 3, res
