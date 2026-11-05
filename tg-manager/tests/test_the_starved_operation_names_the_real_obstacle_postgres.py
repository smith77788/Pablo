"""Операция, не дождавшаяся своей очереди, называет НАСТОЯЩУЮ помеху.

ЧТО БЫЛО. Исполнитель просит повтор (`status: "requeue"`) не только из-за
занятого флота: так же он поступает, когда BotFather ограничил создание ботов и
когда не прочитался список уже обработанных целей (отложить дешевле, чем
отправить промо дважды). Все эти случаи шли одной дорогой, и по истечении
предела ожидания владелец читал: «все аккаунты N мин были заняты другими
операциями». Он шёл разгружать очередь и добавлять аккаунты, а помеха была
совсем другая и никуда не делась.

ЧТО ТЕПЕРЬ. Возврат в очередь запоминает слова исполнителя в `last_error`, а
закрытие по истечении предела их цитирует. От себя код утверждает только то,
что знает точно: сколько ждали и чем кончилось.

Проверяется на живом Postgres: вся механика — два запроса к `operation_queue`
(запомнить причину и прочитать её при закрытии), и на заглушке проверялся бы
текст запроса, а не то, что причина доехала.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889936
BOTFATHER = "BotFather ограничил создание ботов на 3 акк. — попробуйте позже"

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
def clean(pool):
    async def _wipe():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id = $1", OWNER)
    _run(_wipe())
    yield
    _run(_wipe())


async def _running_op(pool) -> int:
    return await pool.fetchval(
        """INSERT INTO operation_queue
               (owner_id, op_type, status, params, total_items, done_items,
                started_at)
           VALUES ($1, 'auto_register', 'running', '{}'::jsonb, 10, 0, now())
        RETURNING id""", OWNER)


async def _starve(pool, op_id: int, reason: str) -> dict:
    """Один возврат в очередь с причиной, затем истёкшее ожидание."""
    from services import op_worker as ow

    await ow._requeue_op_no_accounts(
        pool, op_id, bot=None, owner_id=OWNER, reason=reason)
    # Ожидание «началось полчаса назад»: предел — _ACCT_WAIT_MAX_MIN минут.
    await pool.execute(
        "UPDATE operation_queue SET status='running', "
        "acct_wait_since = now() - make_interval(mins => $2) WHERE id=$1",
        op_id, ow._ACCT_WAIT_MAX_MIN + 10)
    await ow._requeue_op_no_accounts(
        pool, op_id, bot=None, owner_id=OWNER, reason=reason)
    row = await pool.fetchrow(
        "SELECT status, error_msg, result FROM operation_queue WHERE id=$1", op_id)
    return dict(row)


def test_the_obstacle_is_quoted_not_invented(pool):
    """Помеха BotFather не превращается в «аккаунты заняты другими операциями»."""
    op_id = _run(_running_op(pool))
    row = _run(_starve(pool, op_id, BOTFATHER))

    assert row["status"] in ("failed", "partial"), row
    msg = row["error_msg"] or ""
    assert "BotFather" in msg, (
        f"помеху подменили: владелец читает не то, что помешало ({msg!r})")
    assert "заняты другими операциями" not in msg, (
        "операции с ограничением BotFather написали про занятый флот — "
        "владелец пойдёт разгружать очередь, а помеха останется")
    res = json.loads(row["result"]) if row["result"] else {}
    assert "BotFather" in (res.get("summary") or ""), res


def test_the_fleet_case_still_reads_like_the_fleet(pool):
    """Обычный случай не пострадал: занятый флот так и называется."""
    op_id = _run(_running_op(pool))
    row = _run(_starve(
        pool, op_id, "Все аккаунты заняты другой операцией — попробуйте позже"))

    msg = row["error_msg"] or ""
    assert "Все аккаунты заняты другой операцией" in msg, msg
    assert "мин" in msg, "сколько ждали — это как раз то, что код знает точно"


def test_without_a_stored_reason_the_old_meaning_survives(pool):
    """Причины нет — утверждаем прежнее, но не выдумываем новое."""
    from services import op_worker as ow

    async def _go():
        op_id = await _running_op(pool)
        await pool.execute(
            "UPDATE operation_queue SET acct_wait_since = now() - "
            "make_interval(mins => $2), last_error = NULL WHERE id=$1",
            op_id, ow._ACCT_WAIT_MAX_MIN + 10)
        await ow._requeue_op_no_accounts(pool, op_id, bot=None, owner_id=OWNER)
        return await pool.fetchrow(
            "SELECT status, error_msg FROM operation_queue WHERE id=$1", op_id)

    row = _run(_go())
    assert (row["error_msg"] or "").find("флот был занят") >= 0, dict(row)
