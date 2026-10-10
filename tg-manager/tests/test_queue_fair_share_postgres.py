"""Очередь одного владельца не запирает воркер для всех остальных.

ЧТО БЫЛО. Поллер брал окно кандидатов как «самые старые N ожидающих» и только
ПОТОМ, внутри окна, считал ранг по владельцу. Окно маленькое — слоты × (лимит на
владельца + 1), по умолчанию 32. Владелец с очередью длиннее окна занимал его
целиком, и операции остальных в выборку не попадали ВООБЩЕ.

Ограничение «не больше трёх операций на владельца», введённое ради честного
дележа, работало ровно наоборот: длинная очередь одного клиента закрывала воркер
для всех, пока не рассосётся. Массовые операции идут часами — это дни ожидания
для соседа. Причём пять слотов воркера из восьми при этом простаивали: дело не в
нехватке места, а в том, что чужие операции в окно не попадали.

ЧТО ТЕПЕРЬ. Ранг считается по ВСЕЙ очереди ожидающих, и в окно отбираются только
операции, реально имеющие право на запуск.

ПОЧЕМУ ЖИВОЙ POSTGRES. Проверяемое здесь — поведение SQL-запроса: оконная
функция, порядок CTE, FOR UPDATE SKIP LOCKED. Заглушка пула не исполняет SQL и
об этом классе не знает в принципе.

КАК ЗАПУСТИТЬ (2 минуты, Postgres 16) — см. докстринг
tests/test_invite_e2e_postgres.py; переменная та же, INFRAGRAM_TEST_DSN.
Без неё файл пропускается, а ратчет по исходнику внизу работает всегда.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _poller_sql() -> str:
    """Тот самый запрос захвата из op_worker._process_pending, слово в слово."""
    with open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8") as f:
        src = f.read()
    start = src.index("async def _process_pending")
    head = src.index('"""WITH owner_running', start)
    tail = src.index('RETURNING id, owner_id, op_type, params"""', head)
    return src[head + 3:tail] + "RETURNING id, owner_id, op_type, params"


async def _poll(conn, slots, per_owner, window, exclude=None):
    """Позвать запрос поллера так, как его зовёт продукт.

    У запроса есть ЧЕТВЁРТЫЙ параметр — список id операций, которые надо
    пропустить ($4::bigint[]; NULL = не пропускать ничего). Тест звал его с
    тремя аргументами и падал на связывании, но файл выполняется только с живым
    Postgres, которого в CI не было, поэтому расхождение никто не видел.
    """
    return await conn.fetch(_poller_sql(), slots, per_owner, window, exclude)

# ── Ратчет по исходнику: работает и без Postgres ─────────────────────────────

def test_rank_is_computed_before_the_window_not_inside_it():
    sql = _poller_sql()
    ranked = sql.index("ranked AS (")
    candidates = sql.index("candidates AS (")
    assert ranked < candidates, "ранг по владельцу обязан считаться до отбора окна"
    window = sql.index("LIMIT $3")
    assert window > ranked, (
        "окно кандидатов снова обрезает очередь ДО подсчёта ранга: длинная "
        "очередь одного владельца опять закроет воркер для остальных"
    )
    assert "PARTITION BY oq.owner_id" in sql, (
        "ранг считается не по всей очереди ожидающих"
    )
    assert "FOR UPDATE OF oq SKIP LOCKED" in sql, (
        "без SKIP LOCKED две реплики возьмут одну операцию дважды"
    )


# ── Поведение на живом Postgres ──────────────────────────────────────────────

pg = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. докстринг)")

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def conn():
    import asyncpg

    async def _open():
        c = await asyncpg.connect(DSN)
        # Схема накатывается НАСТОЯЩАЯ, всеми schema_v*.sql по возрастанию
        # версии — как в tests/test_invite_e2e_postgres.py. Соблазн создать здесь
        # урезанную operation_queue «только с нужными колонками» проверен и
        # отвергнут: файлы делят одну базу, `CREATE TABLE IF NOT EXISTS` такую
        # заглушку оставляет, и соседний e2e падает на отсутствующих колонках.
        files = sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            try:
                await c.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны; частичный сбой не должен рушить прогон
        return c

    try:
        c = _run(_open())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield c
    _run(c.close())


# Каждый сценарий идёт в ТРАНЗАКЦИИ, которая откатывается. База общая с
# соседними живыми тестами: чужие ожидающие операции съедали бы слоты и счёт
# перестал бы сходиться, а очистить их насовсем нельзя — они чужие. Внутри
# транзакции очередь пуста, снаружи всё на месте.

def _in_tx(conn, seed_sql, *, args=(), extra=(), slots=8, per_owner=3):
    """Очистить очередь, засеять сценарий, выполнить запрос захвата, откатить."""
    window = max(slots * (per_owner + 1), slots)

    async def _body():
        tr = conn.transaction()
        await tr.start()
        try:
            # operation_log ссылается на operation_queue внешним ключом.
            await conn.execute("DELETE FROM operation_log")
            await conn.execute("DELETE FROM operation_queue")
            await conn.execute(seed_sql, *args)
            for sql in extra:
                await conn.execute(sql)
            rows = await _poll(conn, slots, per_owner, window)
            return [(r["id"], r["owner_id"]) for r in rows]
        finally:
            await tr.rollback()

    return _run(_body())


def _by_owner(rows):
    by: dict[int, int] = {}
    for _id, owner in rows:
        by[owner] = by.get(owner, 0) + 1
    return by


@pg
def test_long_queue_of_one_owner_does_not_lock_out_the_others(conn):
    by = _by_owner(_in_tx(
        conn,
        """INSERT INTO operation_queue (owner_id, op_type, status, created_at)
           SELECT 1, 'bulk_join', 'pending', now() - make_interval(secs => 100000 - g)
           FROM generate_series(1, 100) g;""",
        extra=("INSERT INTO operation_queue (owner_id, op_type, status, created_at) "
               "VALUES (2, 'bulk_join', 'pending', now() - interval '1 minute')",),
    ))
    assert by.get(2) == 1, (
        "операция второго владельца не попала в выборку: очередь первого "
        f"закрыла воркер целиком (взято {by})"
    )
    assert by.get(1) == 3, f"лимит на владельца нарушен: {by}"


@pg
def test_per_owner_limit_counts_what_is_already_running(conn):
    rows = _in_tx(
        conn,
        """INSERT INTO operation_queue (owner_id, op_type, status, created_at)
           SELECT 1, 'bulk_join', 'pending', now() - make_interval(secs => 100 - g)
           FROM generate_series(1, 10) g;""",
        extra=("INSERT INTO operation_queue (owner_id, op_type, status, created_at, started_at) "
               "SELECT 1, 'bulk_join', 'running', now(), now() FROM generate_series(1, 3) g;",),
    )

    assert _by_owner(rows) == {}, "владелец, у которого уже три в работе, получил четвёртую"


@pg
def test_deferred_and_unapproved_operations_are_not_picked(conn):
    rows = _in_tx(
        conn,
        """INSERT INTO operation_queue (owner_id, op_type, status, created_at, scheduled_for)
           VALUES (1, 'bulk_join', 'pending', now(), now() + interval '1 hour');""",
        extra=(
            "INSERT INTO operation_queue (owner_id, op_type, status, created_at, requires_approval) "
            "VALUES (2, 'bulk_join', 'pending', now(), true)",
            "INSERT INTO operation_queue (owner_id, op_type, status, created_at) "
            "VALUES (3, 'bulk_join', 'pending', now())",
        ),
    )

    assert _by_owner(rows) == {3: 1}, (
        "в работу ушла операция, которая ждёт своего времени или подтверждения"
    )


@pg
def test_two_pollers_never_take_the_same_operation(conn):
    """И при этом каждая реплика что-то берёт.

    Старый запрос блокировал ВСЁ окно (32 строки) ещё до отбора, а забирал из
    него не больше восьми. Соседняя реплика натыкалась на замки и уходила ни с
    чем, хотя работы в очереди хватало на обеих. Новый запрос блокирует ровно
    то, что берёт.
    """
    import asyncpg

    # Здесь строки должны быть ВИДНЫ второму соединению, поэтому сценарий не
    # откатывается, а прибирает за собой сам. Свой диапазон владельцев — база
    # общая с соседними живыми тестами.
    owners = list(range(990101, 990121))

    async def _race():
        other = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO operation_queue (owner_id, op_type, status, created_at) "
                "SELECT o, 'bulk_join', 'pending', now() - make_interval(secs => 100 - o - 990100) "
                "FROM unnest($1::bigint[]) o", owners)
            tr = conn.transaction()
            await tr.start()
            mine = await _poll(conn, 8, 3, 32)
            theirs = await _poll(other, 8, 3, 32)
            await tr.commit()
            return {r["id"] for r in mine}, {r["id"] for r in theirs}
        finally:
            await other.close()
            await conn.execute(
                "DELETE FROM operation_queue WHERE owner_id = ANY($1::bigint[])", owners)

    mine, theirs = _run(_race())
    assert mine and theirs, (
        "одна из реплик не взяла ничего: старый запрос блокировал всё окно "
        "целиком, и соседу не доставалось работы, которой в очереди хватало"
    )
    assert not (mine & theirs), (
        f"обе реплики взяли одни и те же операции {mine & theirs}: одна и та же "
        "массовая операция пойдёт дважды по одним аккаунтам"
    )
