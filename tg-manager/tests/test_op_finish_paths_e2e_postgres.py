"""Запросы путей завершения операции — по НАСТОЯЩЕМУ Postgres.

ЗАЧЕМ ОТДЕЛЬНО. Заглушка пула не проверяет типы параметров: фейковый
`fetchrow(q, *a)` принимает что угодно, поэтому ошибки СВЯЗЫВАНИЯ (строка вместо
datetime, dict вместо jsonb) на юнит-тестах невидимы в принципе — ровно так
пережил релиз сломанный `operation_bus.submit(scheduled_for=…)`. Здесь проверены
запросы, которые добавились в путях завершения и в гейте одиночных операций:
`EXTRACT`/`GREATEST` над `cooldown_until`, `FILTER`/`bool_or` над журналом целей,
запись `result` в jsonb вместе с защитой терминального статуса.

КАК ЗАПУСТИТЬ (2 минуты, Postgres 16) — рецепт тот же, что в
tests/test_invite_e2e_postgres.py:

    D=/var/tmp/pgtest; mkdir -p $D/pg $D/sock; chown -R postgres $D
    su postgres -s /bin/bash -c "initdb -D $D/pg -U postgres -A trust"
    su postgres -s /bin/bash -c "pg_ctl -D $D/pg \\
        -o \\"-p 55432 -k $D/sock -c listen_addresses=''\\" -l $D/pg/log start"
    psql -h $D/sock -p 55432 -U postgres -c 'CREATE DATABASE infra'
    export INFRAGRAM_TEST_DSN="postgresql://postgres@/infra?host=$D/sock&port=55432"
    pytest tests/test_op_finish_paths_e2e_postgres.py -v

Без переменной окружения файл пропускается — CI и обычный прогон не ломаются.
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
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER = 990777

# ОДИН цикл на весь модуль: соединения asyncpg привязаны к циклу, в котором
# создан пул, и new_event_loop() на каждый вызов даёт «Event loop is closed».
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
                pass  # схемы идемпотентны; частичный сбой не рушит прогон
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


async def _new_op(pool, status="running", **cols) -> int:
    keys = ["owner_id", "op_type", "params", "status"] + list(cols)
    vals = [OWNER, "mass_publish", json.dumps({}), status] + list(cols.values())
    ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
    return await pool.fetchval(
        f"INSERT INTO operation_queue({', '.join(keys)}) VALUES({ph}) RETURNING id",
        *vals)


async def _new_acc(pool, **cols) -> int:
    keys = ["owner_id", "phone", "session_str", "is_active"] + list(cols)
    vals = [OWNER, f"+79{os.urandom(4).hex()}", "s", True] + list(cols.values())
    ph = ", ".join(f"${i + 1}" for i in range(len(vals)))
    return await pool.fetchval(
        f"INSERT INTO tg_accounts({', '.join(keys)}) VALUES({ph}) RETURNING id",
        *vals)


# ── Гейт одиночных операций: EXTRACT/GREATEST над cooldown_until ─────────────

def test_paused_account_is_read_from_the_database(pool):
    from services import op_worker

    async def _go():
        acc_id = await _new_acc(
            pool, cooldown_until=await pool.fetchval(
                "SELECT now() + interval '30 minutes'"))
        return await op_worker._single_account_parked(pool, acc_id)

    res = _run(_go())
    assert res and res["status"] == "requeue", res
    assert 1800 <= res["defer_s"] <= 1800 + 120, res


def test_dead_status_is_read_from_the_database(pool):
    from services import op_worker

    async def _go():
        acc_id = await _new_acc(pool, acc_status="spamblock")
        return await op_worker._single_account_parked(pool, acc_id)

    res = _run(_go())
    assert res and res["status"] == "failed", res
    assert "spamblock" in res["reason"]


def test_healthy_account_passes_the_gate(pool):
    from services import op_worker

    async def _go():
        acc_id = await _new_acc(pool)
        return await op_worker._single_account_parked(pool, acc_id)

    assert _run(_go()) is None


def test_expired_pause_does_not_hold_the_account(pool):
    """GREATEST(0, …): истёкший кулдаун обязан дать 0, а не отрицательное число."""
    from services import op_worker

    async def _go():
        acc_id = await _new_acc(
            pool, cooldown_until=await pool.fetchval(
                "SELECT now() - interval '5 minutes'"))
        return await op_worker._single_account_parked(pool, acc_id)

    assert _run(_go()) is None


# ── Счётчики по журналу: FILTER/bool_or/GROUP BY ─────────────────────────────

def test_journal_counters_on_real_sql(pool):
    from services import op_worker

    async def _go():
        op_id = await _new_op(pool)
        rows = [
            ("a", "ok"),       # чистый успех
            ("b", "error"),    # чистый провал
            ("c", "error"),    # цель с ретраем внутри прогона:
            ("c", "ok"),       #   поздний успех обязан перевесить
            ("d", "info"),     # примечание исполнителя — не приговор цели
        ]
        for i, (t, st) in enumerate(rows):
            await pool.execute(
                "INSERT INTO operation_log(op_id, step_num, target, status) "
                "VALUES($1,$2,$3,$4)", op_id, i, t, st)
        return await op_worker._journal_counters(pool, op_id)

    assert _run(_go()) == (2, 1)


def test_empty_journal_counts_nothing(pool):
    from services import op_worker

    async def _go():
        return await op_worker._journal_counters(pool, await _new_op(pool))

    assert _run(_go()) == (0, 0)


# ── Закрытие отменённой операции: jsonb + защита статуса ─────────────────────

def test_cancelled_finish_writes_the_result(pool):
    from services import op_worker

    async def _go():
        op_id = await _new_op(pool, acct_wait_since=await pool.fetchval("SELECT now()"))
        await op_worker._finish_cancelled_op(
            pool, op_id, OWNER, "mass_publish", {},
            {"status": "cancelled", "ok": 7, "failed": 2,
             "summary": "Отменено. Опубликовано: 7"}, 12.5)
        return await pool.fetchrow(
            "SELECT status, finished_at, acct_wait_since, result->>'ok' AS ok, "
            "result->>'summary' AS summary FROM operation_queue WHERE id=$1", op_id)

    row = _run(_go())
    assert row["status"] == "cancelled"
    assert row["finished_at"] is not None
    assert row["acct_wait_since"] is None, "ожидание флота не сброшено — повтор упадёт"
    assert row["ok"] == "7" and "7" in row["summary"]


def test_cancelled_finish_keeps_an_owner_cancellation(pool):
    """Владелец нажал «Отменить» раньше: статус тот же, итог дописывается."""
    from services import op_worker

    async def _go():
        op_id = await _new_op(pool, status="cancelled")
        await op_worker._finish_cancelled_op(
            pool, op_id, OWNER, "mass_publish", {},
            {"status": "cancelled", "ok": 3, "failed": 0}, 1.0)
        return await pool.fetchrow(
            "SELECT status, result->>'ok' AS ok FROM operation_queue WHERE id=$1", op_id)

    row = _run(_go())
    assert row["status"] == "cancelled" and row["ok"] == "3"


def test_cancelled_finish_never_resurrects_a_closed_operation(pool):
    """Гонка: пока исполнитель сворачивался, операция успела закрыться успехом."""
    from services import op_worker

    async def _go():
        op_id = await _new_op(pool, status="done")
        await op_worker._finish_cancelled_op(
            pool, op_id, OWNER, "mass_publish", {}, {"status": "cancelled"}, 1.0)
        return await pool.fetchval(
            "SELECT status FROM operation_queue WHERE id=$1", op_id)

    assert _run(_go()) == "done"


# ── Закрытие упавшей операции ────────────────────────────────────────────────

def _crash_update_sql() -> str:
    """Собрать ТОТ ЖЕ запрос, которым путь исключения закрывает операцию.

    Запрос живёт внутри `_run_op_task` и отдельной функцией не вынесен, поэтому
    здесь он собирается из тех же кусков — и каждый кусок сверяется с
    исходником. Разъехалось — тест падает и говорит, что запрос изменился и его
    надо перепроверить на живой БД, а не молча проверяет свою копию.
    """
    from services import op_status

    parts = [
        "UPDATE operation_queue SET status=$3, finished_at=now(), error_msg=$1, ",
        "result=$4::jsonb, acct_wait_since=NULL ",
    ]
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "services", "op_worker.py"),
        encoding="utf-8").read()
    for p in parts:
        assert p in src, (
            f"запрос пути исключения изменился, кусок не найден: {p!r} — "
            "перепроверьте его на живой БД и обновите этот тест")
    return "".join(parts) + (
        f"WHERE id=$2 AND status NOT IN {op_status.sql_terminal_list()}")


def test_crash_finish_writes_status_and_result(pool):
    async def _go():
        op_id = await _new_op(pool, acct_wait_since=await pool.fetchval("SELECT now()"))
        res = await pool.execute(
            _crash_update_sql(), "сеть отвалилась", op_id, "partial",
            json.dumps({"status": "partial", "ok": 150, "failed": 0},
                       ensure_ascii=False))
        row = await pool.fetchrow(
            "SELECT status, error_msg, acct_wait_since, result->>'ok' AS ok "
            "FROM operation_queue WHERE id=$1", op_id)
        return res, row

    res, row = _run(_go())
    assert res.strip().endswith(" 1"), res
    assert row["status"] == "partial" and row["ok"] == "150"
    assert row["error_msg"] == "сеть отвалилась"
    assert row["acct_wait_since"] is None


def test_crash_finish_reports_zero_rows_on_a_cancelled_operation(pool):
    """Ноль строк — признак, по которому путь исключения узнаёт отмену владельца
    и НЕ докладывает о провале. Если запрос перестанет его давать, владелец снова
    будет получать «ошибка» на то, что сам остановил."""
    async def _go():
        op_id = await _new_op(pool, status="cancelled")
        return await pool.execute(
            _crash_update_sql(), "сеть отвалилась", op_id, "failed",
            json.dumps({"status": "failed"}))

    assert _run(_go()).strip().endswith(" 0")
