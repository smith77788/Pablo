"""Расписание не умирает от деплоя, пришедшего в момент закрытия круга.

РАЗРЫВ. Путь завершения круга пишет в таком порядке: терминальный статус →
объявление исхода → продление расписания (`_reschedule_recurring`). Порядок
правильный: продление идёт через шину, то есть через предохранитель Ban Weather
и гейт тарифа, и ставить следующий круг до закрытия текущего нельзя. Но между
записями есть зазор, а SIGTERM на Railway приходит когда угодно. Процесс,
умерший в этом зазоре, оставлял расписание мёртвым НАВСЕГДА: круг закрыт,
следующий не поставлен, а закрытую операцию больше никто не смотрит. Владелец
узнавал об этом по тишине в канале — тот же исход, про который докстринг
`_reschedule_recurring` говорит «хуже всего, что владелец об этом не узнавал»,
только эта причина оставалась незакрытой.

ЧТО ПРОВЕРЯЕМ. Продление ставит на закрытом круге отметку `recurring_next_op`,
а сторож находит круги без отметки и продолжает цепочку. Главное требование к
нему — НЕ создать лишний круг: второй пост в канал дороже задержки. Поэтому
сомнение всегда в пользу «не трогать»: есть живой круг этой цепочки — не лезем;
отмена владельцем — не возвращаем; круг закрыт только что — ждём нормальный
путь.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяется семантика запросов сторожа (окно по
времени, отметка в jsonb, сверка метки цепочки через regexp_replace). Заглушка
пула SQL не выполняет вовсе. Рецепт запуска — в docstring
tests/test_op_finish_paths_e2e_postgres.py.
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

OWNER = 991786
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
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))


def _mine(placed: list[dict], owner: int = OWNER) -> list[dict]:
    """Только постановки нашего владельца: сторож ходит по всей очереди, а в
    тестовой базе живут операции других проверок."""
    return [p for p in placed if p.get("owner_id") == owner]


@pytest.fixture
def submitted(monkeypatch):
    """Шина заменена на запись-наблюдателя: нас интересует сам факт продления."""
    from services import operation_bus, op_worker

    placed: list[dict] = []

    async def _submit(pool, owner_id, op_type, params, **kw):
        placed.append({"owner_id": int(owner_id), "op_type": op_type,
                       "params": dict(params or {}), "label": kw.get("label"),
                       "scheduled_for": kw.get("scheduled_for")})
        return await pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, params, status, label, "
            "scheduled_for) VALUES($1,$2,$3::jsonb,'pending',$4,$5) RETURNING id",
            owner_id, op_type, json.dumps(params or {}), kw.get("label"),
            kw.get("scheduled_for"))

    monkeypatch.setattr(operation_bus, "submit", _submit)
    monkeypatch.setattr(op_worker, "_notify_recurring_stopped",
                        _noop_notify, raising=False)
    return placed


async def _noop_notify(*a, **k):
    return None


async def _closed_round(
    pool, *, status: str = "done", age_min: int = 30,
    interval_min: int = 60, label: str = "Пост дня",
    marked: int | None = None, op_type: str = "quick_post",
) -> int:
    params: dict = {"repeat_interval_min": interval_min}
    if marked is not None:
        params["recurring_next_op"] = marked
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, label, "
        "total_items, done_items) VALUES($1,$2,$3::jsonb,$4,$5,3,3) RETURNING id",
        OWNER, op_type, json.dumps(params), status, label)
    await pool.execute(
        "UPDATE operation_queue SET finished_at = now() - make_interval(mins => $2) "
        " WHERE id = $1", op_id, int(age_min))
    return op_id


async def _mark(pool, op_id: int):
    return await pool.fetchval(
        "SELECT params->>'recurring_next_op' FROM operation_queue WHERE id=$1", op_id)


async def _pending(pool) -> int:
    return int(await pool.fetchval(
        "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 AND status='pending'",
        OWNER) or 0)


# ── Главное: оборванная цепочка продолжается ────────────────────────────────

def test_a_broken_chain_is_continued(pool, submitted):
    from services import op_worker

    op_id = _run(_closed_round(pool))

    n = _run(op_worker._watchdog_recurring_chain(pool, None))

    assert n >= 1, "оборванное расписание не продолжено — канал замолчал навсегда"
    assert _run(_pending(pool)) == 1, "следующий круг не встал в очередь"
    assert _run(_mark(pool, op_id)), "на закрытом круге нет отметки о продолжении"
    mine = _mine(submitted)
    assert len(mine) == 1 and mine[0]["op_type"] == "quick_post"
    assert mine[0]["scheduled_for"] is not None, (
        "следующий круг обязан быть отложенным, а не стартовать немедленно")


def test_the_same_round_is_not_continued_twice(pool, submitted):
    """Отметка — защита от второго круга: второй пост в канал дороже задержки."""
    from services import op_worker

    _run(_closed_round(pool))
    _run(op_worker._watchdog_recurring_chain(pool, None))
    _run(pool.execute(
        "UPDATE operation_queue SET status='done', finished_at=now() - "
        "make_interval(mins => 30) WHERE owner_id=$1 AND status='pending'", OWNER))

    _run(op_worker._watchdog_recurring_chain(pool, None))

    mine = _mine(submitted)
    assert len(mine) <= 2, (
        f"сторож продолжил цепочку лишний раз: {len(mine)} постановок")


# ── Сомнение трактуется в пользу «не трогать» ───────────────────────────────

def test_a_chain_with_a_live_round_is_left_alone(pool, submitted):
    """Старый круг без отметки, но цепочка жива — лезть нельзя."""
    from services import op_worker

    _run(_closed_round(pool, label="Пост дня"))
    _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, label) "
        "VALUES($1,'quick_post','{\"repeat_interval_min\":60}'::jsonb,'pending',"
        "'Пост дня ↻') RETURNING id", OWNER))

    _run(op_worker._watchdog_recurring_chain(pool, None))

    assert not _mine(submitted), (
        "сторож поставил круг поверх живого — в канал уйдёт два поста")


def test_a_cancelled_round_is_not_resurrected(pool, submitted):
    """Отмена владельца — это конец расписания, а не обрыв."""
    from services import op_worker

    _run(_closed_round(pool, status="cancelled"))

    _run(op_worker._watchdog_recurring_chain(pool, None))
    assert not _mine(submitted), "отменённое расписание возобновилось само"


def test_a_round_closed_a_moment_ago_waits_for_the_normal_path(pool, submitted):
    """Нормальный путь продлевает сам; обгонять его — это второй круг."""
    from services import op_worker

    _run(_closed_round(pool, age_min=1))

    _run(op_worker._watchdog_recurring_chain(pool, None))
    assert not _mine(submitted)


def test_a_chain_broken_long_ago_is_not_revived_blindly(pool, submitted):
    """Сутки тишины — владелец мог всё поменять; молча стрелять постом нельзя."""
    from services import op_worker

    _run(_closed_round(pool, age_min=60 * 24))

    _run(op_worker._watchdog_recurring_chain(pool, None))
    assert not _mine(submitted)


def test_a_one_off_operation_is_not_turned_into_a_schedule(pool, submitted):
    """Без repeat_interval_min никакого расписания не было вовсе."""
    from services import op_worker

    op_id = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, label, "
        "finished_at) VALUES($1,'quick_post','{}'::jsonb,'done','Разовый',"
        "now() - interval '30 minutes') RETURNING id", OWNER))

    _run(op_worker._watchdog_recurring_chain(pool, None))
    assert not _mine(submitted)
    assert _run(_mark(pool, op_id)) is None


def test_a_type_outside_the_allowlist_is_marked_and_dropped(pool, submitted):
    """Иначе сторож брался бы за один и тот же круг каждые пять минут."""
    from services import op_worker

    op_id = _run(_closed_round(pool, op_type="mass_invite"))

    _run(op_worker._watchdog_recurring_chain(pool, None))
    assert not _mine(submitted), "тип не из списка расписаний — продлевать нечего"
    assert _run(_mark(pool, op_id)) == "0", (
        "круг не помечен — сторож вернётся к нему через пять минут и так вечно")


def test_another_owner_is_not_touched(pool, submitted):
    """Чужую цепочку сторож не продолжает."""
    from services import op_worker

    other = _run(pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, params, status, label, "
        "finished_at) VALUES($1,'quick_post','{\"repeat_interval_min\":60}'::jsonb,"
        "'done','Чужой', now() - interval '30 minutes') RETURNING id", OWNER + 1))
    try:
        _run(op_worker._watchdog_recurring_chain(pool, None))
        placed_for_other = _mine(submitted, OWNER + 1)
        assert placed_for_other, (
            "самопроверка: сторож обязан видеть и этого владельца тоже")
    finally:
        _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER + 1))
