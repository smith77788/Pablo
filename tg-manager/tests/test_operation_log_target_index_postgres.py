"""Окно активности аккаунта в operation_log — на НАСТОЯЩЕМ Postgres.

Две вещи, которых заглушка пула не покажет в принципе.

Первое: план запроса. `operation_log` — самая большая таблица продукта (строка
на каждую цель каждой массовой операции), а движок иммунитета читает её по
`target`. Ведущей колонки `target` не было ни в одном индексе, и каждое
событие смерти аккаунта читало таблицу целиком; заметно это только на живой
базе с данными.

Второе: типы параметров. `_gather_features` передаёт время в `$2::timestamptz`
и глотает любое исключение (fail-soft по замыслу — разбор смерти не должен
ронять цикл). Ошибка связывания на юнит-тестах невидима, а в проде окно просто
всегда оказывалось бы пустым.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py (нужен
INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import asyncio
import datetime
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN"
)

_OP_ID = 999_228          # свой номер операции, чтобы не мешать другим тестам
_ACC_ID = 999_229
_OWNER = 999_230
_ROWS = 20_000
# Операций в очереди тоже должно быть МНОГО. План этого запроса выбирается из
# двух: зайти со стороны журнала по индексу (target, created_at) или со стороны
# operation_queue и для каждой операции лезть в журнал по op_id. Второй путь
# дёшев ровно до тех пор, пока очередь маленькая, — а на свежей базе в ней
# десяток строк, и планировщик честно берёт именно его. Тест при этом падал,
# «обнаружив» потерю индекса, которого никто не терял. Сеем очередь сами, а не
# надеемся на то, что лежит в базе от других прогонов.
_QUEUE_ROWS = 5_000
_QUEUE_ID_BASE = 999_300_000

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def conn():
    import asyncpg

    try:
        c = _run(asyncpg.connect(DSN))
    except Exception as exc:
        pytest.skip(f"нет Postgres по INFRAGRAM_TEST_DSN: {exc}")

    async def _seed():
        await c.execute("DELETE FROM operation_log WHERE op_id=$1", _OP_ID)
        await c.execute("DELETE FROM operation_queue WHERE id=$1", _OP_ID)
        await c.execute(
            "INSERT INTO operation_queue(id, owner_id, op_type, status) "
            "VALUES($1,$2,'mass_invite','done')", _OP_ID, _OWNER)
        # Тысяча разных целей: на одну цель приходится двадцатая доля процента
        # строк — ровно тот случай, когда индекс обязан выиграть у чтения всей
        # таблицы.
        await c.execute(
            """INSERT INTO operation_log(op_id, step_num, target, status, created_at)
               SELECT $1, i, (i % 1000)::text,
                      CASE WHEN i % 7 = 0 THEN 'error' ELSE 'ok' END,
                      now() - INTERVAL '1 hour'
                 FROM generate_series(1, $2) i""", _OP_ID, _ROWS)
        await c.execute(
            "DELETE FROM operation_queue WHERE id >= $1 AND id < $2",
            _QUEUE_ID_BASE, _QUEUE_ID_BASE + _QUEUE_ROWS)
        await c.execute(
            """INSERT INTO operation_queue(id, owner_id, op_type, status)
               SELECT $1 + i, $2, 'mass_invite', 'done'
                 FROM generate_series(0, $3 - 1) i""",
            _QUEUE_ID_BASE, _OWNER, _QUEUE_ROWS)
        await c.execute("ANALYZE operation_log")
        await c.execute("ANALYZE operation_queue")

    _run(_seed())
    yield c

    async def _cleanup():
        await c.execute("DELETE FROM operation_log WHERE op_id=$1", _OP_ID)
        await c.execute("DELETE FROM operation_queue WHERE id=$1", _OP_ID)
        await c.execute(
            "DELETE FROM operation_queue WHERE id >= $1 AND id < $2",
            _QUEUE_ID_BASE, _QUEUE_ID_BASE + _QUEUE_ROWS)
        await c.close()

    _run(_cleanup())


def test_индекс_по_цели_существует(conn):
    """Миграция schema_v229 доехала и индекс ведёт по target."""
    rows = _run(conn.fetch(
        "SELECT indexdef FROM pg_indexes WHERE tablename='operation_log'"))
    defs = [r["indexdef"] for r in rows]
    ведущий_target = [d for d in defs if "(target" in d.replace(" (", "(")]
    assert ведущий_target, defs


def test_окно_по_цели_идёт_индексом_по_цели(conn):
    """Окно активности берётся индексом по target, а не перебором журнала.

    Форма запроса повторяет `immunity_engine._gather_features`. Без индекса по
    target план выглядит обманчиво прилично: планировщик заходит со стороны
    operation_queue и для КАЖДОЙ операции лезет в журнал по op_id, отбрасывая
    почти всё фильтром по цели. На живой базе операций тысячи, и стоимость
    такого плана на два порядка выше — поэтому проверяем не отсутствие Seq
    Scan, а что в плане стоит именно индекс по цели.
    """
    at = datetime.datetime.now(datetime.timezone.utc)
    plan = _run(conn.fetch(
        """EXPLAIN
           SELECT q.op_type, COUNT(*) AS c,
                  COUNT(*) FILTER (WHERE l.status = 'error') AS errors
             FROM operation_log l
             JOIN operation_queue q ON q.id = l.op_id
            WHERE l.target = $1
              AND l.created_at > $2::timestamptz - INTERVAL '72 hours'
              AND l.created_at <= $2::timestamptz
            GROUP BY q.op_type""", "17", at))
    text = "\n".join(r["QUERY PLAN"] for r in plan)
    assert "idx_operation_log_target_created" in text, text


def test_окно_активности_собирается_на_живой_схеме(conn):
    """`_gather_features` действительно возвращает окно, а не глотает ошибку.

    Проверяем на цели «17»: строк по ней ровно двадцать (20 000 / 1000), из них
    неудачных — те, чей номер делится на семь.
    """
    from services import immunity_engine

    at = datetime.datetime.now(datetime.timezone.utc)
    # Без фильтра по op_id: функция считает ВСЕ строки по цели в окне, и в
    # общей тестовой базе по той же цели могут лежать строки других тестов.
    ожидалось = _run(conn.fetchval(
        """SELECT COUNT(*) FROM operation_log
            WHERE target='17'
              AND created_at > $1::timestamptz - INTERVAL '72 hours'
              AND created_at <= $1::timestamptz""", at))
    assert ожидалось > 0, "подготовка теста не записала строк по цели"

    feats = _run(immunity_engine._gather_features(conn, 17, _OWNER, at))

    assert feats["actions_72h"] == ожидалось, feats
    assert sum(feats["op_counts"].values()) == ожидалось, feats
    assert feats["op_counts"].get("mass_invite"), feats
    assert 0.0 < feats["error_rate"] < 1.0, feats
