"""Бюджет живучести тратится на воскрешения БЕЗ продвижения — на живом Postgres.

ЧТО БЫЛО. `operation_queue.revive_count` росла на каждом воскрешении и не
обнулялась нигде, а на `_MAX_REVIVES` (по умолчанию 3) операция гасилась как
«ядовитая». Но воскрешение случается не только от ядовитой операции: рестарт
процесса возвращает в очередь ВСЁ, что было в работе, а деплой здесь едет на
каждый пуш в рабочую ветку. Значит длинная честная операция (инвайт идёт
часами, потолок прогона — 6 часов) переживала три деплоя и получала
«❌ Остановлена: 3 раз(а) прерывалась». Владельцу при этом писали «прерывалась
и НЕ ПРОДВИГАЛАСЬ» — слово, которого код не проверял ни разу.

ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ НА БАЗЕ. Решение целиком в SQL: `CASE` сравнивает
`done_items` с отметкой `revive_mark`, `GREATEST` поднимает отметку, а гашение
смотрит на два предела сразу. На заглушке проверяется текст запроса, а не его
смысл; здесь — именно смысл, включая то, что `done_items` обнуляется ПОСЛЕ
отметки, а не до.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889935

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

    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

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
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def only_our_rows(pool, monkeypatch):
    """Свои строки убираем, чужие 'running' не трогаем.

    `_reset_stale_running` ходит по ВСЕЙ таблице, поэтому оставленная соседним
    тестом строка в 'running' попала бы под гашение и увела бы счёт. Поэтому
    перед прогоном чужие 'running' уводим в сторону, а после возвращаем.
    """
    from services import op_worker as ow

    async def _prep():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id = $1", OWNER)
        return await pool.fetch(
            "UPDATE operation_queue SET status = 'paused' "
            " WHERE status = 'running' AND owner_id <> $1 RETURNING id", OWNER)

    foreign = _run(_prep())

    # Гашение дозакрывается отдельной функцией (журнал, подпись, слово
    # владельцу). Здесь проверяется SQL, а не она, поэтому подменяем её
    # записью вызовов: иначе тест проверял бы уведомления.
    seen: list[dict] = []

    async def _fake_finish(pool_, row, *, source, bot=None):
        seen.append(dict(row))

    monkeypatch.setattr(ow, "_finish_poisoned_op", _fake_finish)
    yield seen

    async def _cleanup():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id = $1", OWNER)
        for r in foreign or []:
            await pool.execute(
                "UPDATE operation_queue SET status = 'running' WHERE id = $1", r["id"])
    _run(_cleanup())


async def _op(pool, *, done, mark, count, total=0) -> int:
    return await pool.fetchval(
        """INSERT INTO operation_queue
               (owner_id, op_type, status, params, total_items, done_items,
                started_at, revive_count, revive_mark, revive_total)
           VALUES ($1, 'mass_invite', 'running', '{}'::jsonb, 100, $2,
                   now() - interval '2 hours', $3, $4, $5)
        RETURNING id""",
        OWNER, done, count, mark, total)


def _row(pool, op_id):
    return _run(pool.fetchrow(
        "SELECT status, done_items, revive_count, revive_mark, revive_total, "
        "       error_msg FROM operation_queue WHERE id = $1", op_id))


def test_a_run_that_moved_forward_gets_its_budget_back(pool):
    """Сделала новую работу — счётчик подряд идущих обнулён, операция живёт."""
    from services import op_worker as ow

    op_id = _run(_op(pool, done=9, mark=5, count=2))
    _run(ow._reset_stale_running(pool, bot=None))
    row = _row(pool, op_id)

    assert row["status"] == "pending", (
        "продвигавшаяся операция не вернулась в очередь")
    assert row["revive_count"] == 0, (
        "счётчик подряд идущих воскрешений не обнулён продвижением: три деплоя "
        "погасят длинную честную операцию")
    assert row["revive_mark"] == 9, "отметка продвижения не поднялась до достигнутого"
    assert row["revive_total"] == 1, "общий счёт воскрешений не вырос"
    assert row["done_items"] == 0, "счётчик прогона не обнулён — он копился бы поверх"


def test_a_run_that_did_nothing_new_spends_the_budget(pool):
    """Ничего нового не сделала — бюджет тратится, как и раньше."""
    from services import op_worker as ow

    op_id = _run(_op(pool, done=5, mark=5, count=2))
    _run(ow._reset_stale_running(pool, bot=None))
    row = _row(pool, op_id)

    assert row["status"] == "pending"
    assert row["revive_count"] == 3, (
        f"бюджет не истратился на пустое воскрешение ({dict(row)})")
    assert row["revive_mark"] == 5


def test_the_poison_door_still_closes(pool, only_our_rows):
    """Исчерпавшая бюджет операция гасится, а причина — про непродвижение."""
    from services import op_worker as ow

    op_id = _run(_op(pool, done=5, mark=5, count=ow._MAX_REVIVES))
    _run(ow._reset_stale_running(pool, bot=None))
    row = _row(pool, op_id)

    assert row["status"] == "failed", "ядовитая операция пошла на очередной круг"
    assert "не дошла до конца" in (row["error_msg"] or ""), row["error_msg"]
    assert [r["id"] for r in only_our_rows] == [op_id], (
        "гашение не дозакрыли: владелец увидит исход без цифр")


def test_a_progressing_operation_still_has_an_absolute_limit(pool, only_our_rows):
    """Продвигается, но воскрешали слишком много раз — гасится своими словами.

    Без этого предела операция, делающая одну цель и снова роняющая воркер,
    обнуляла бы счётчик подряд идущих и жила вечно, забирая слот владельца.
    """
    from services import op_worker as ow

    op_id = _run(_op(pool, done=90, mark=5, count=0,
                     total=ow._MAX_REVIVES_TOTAL))
    _run(ow._reset_stale_running(pool, bot=None))
    row = _row(pool, op_id)

    assert row["status"] == "failed", (
        f"абсолютный предел не сработал ({dict(row)})")
    msg = row["error_msg"] or ""
    assert "воскрешали" in msg, msg
    assert "не продвигалась" not in msg, (
        "продвигавшейся операции написали, что она не продвигалась — ровно то "
        "вранье, из-за которого предел и переписан")
    assert [r["id"] for r in only_our_rows] == [op_id]


def test_the_owner_text_of_the_second_limit_does_not_claim_a_stall():
    """Слово владельцу у второго предела — своё, а не чужое.

    Читаем файл, а не `inspect.getsource`: автоматическая подмена
    `_finish_poisoned_op` в фикстуре этого модуля отдала бы исходник заглушки,
    и проверка прошла бы, ничего не проверив. На этом и попались.
    """
    src = open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "services", "op_worker.py"), encoding="utf-8").read()
    body = src[src.index("async def _finish_poisoned_op("):]
    body = body[:body.index("\nasync def _reset_stale_running(")]
    assert "_POISON_ERROR_TOTAL" in body and "_by_total" in body, (
        "второй предел объясняется владельцу текстом первого")
    total_branch = body[body.index("_by_total"):]
    assert "воскрешали" in total_branch
