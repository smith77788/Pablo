"""Журнал доставок рассылки проверяется на живом Postgres.

Докатка оборвавшихся рассылок идёт на каждом рестарте процесса и держится на
одном: журнал доставок говорит, кому уже написали. На заглушке это не
проверяется никак — стенд ничего не хранит, и `ON CONFLICT DO NOTHING`
«срабатывает» всегда.

Что доказывается на самой базе:

  * повторная запись той же доставки не создаёт второй строки (PRIMARY KEY
    (broadcast_id, user_id) и есть дедуп, а не украшение);
  * журнал ОДНОЙ рассылки другой рассылке не указ: вторая строка в
    `broadcasts` начинает с пустым журналом и написала бы той же аудитории
    заново. Это и есть причина, по которой закрепление id рассылки за
    операцией — цена запуска, а не пометка рядом с ним
    (`tests/test_a_broadcast_is_not_launched_unremembered.py`);
  * чтение журнала отдаёт ровно то, что записано, и ничего из чужих
    рассылок.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889933
BOT = 778899

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


async def _new_broadcast(pool) -> int:
    return int(await pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, total_users, created_by) "
        "VALUES($1,'Привет',3,$2) RETURNING id", BOT, OWNER))


@pytest.fixture
def two_broadcasts(pool):
    """Две строки рассылки одного бота — ровно то, что заводит повтор без id."""
    async def _mk():
        await pool.execute("DELETE FROM broadcasts WHERE created_by=$1", OWNER)
        await pool.execute("DELETE FROM managed_bots WHERE added_by=$1", OWNER)
        # Строка рассылки ссылается на бота — без него FK не пустит.
        await pool.execute(
            "INSERT INTO managed_bots(token, bot_id, added_by) VALUES($1,$2,$3)",
            "stand-token", BOT, OWNER)
        return await _new_broadcast(pool), await _new_broadcast(pool)

    first, second = _run(_mk())
    yield first, second
    _run(pool.execute("DELETE FROM broadcasts WHERE created_by=$1", OWNER))
    _run(pool.execute("DELETE FROM managed_bots WHERE added_by=$1", OWNER))


async def _journal(pool, bid, *uids):
    await pool.executemany(
        "INSERT INTO broadcast_delivery_log(broadcast_id, user_id) "
        "VALUES($1,$2) ON CONFLICT DO NOTHING",
        [(bid, u) for u in uids])


def test_the_same_delivery_is_never_journalled_twice(pool, two_broadcasts):
    from database import db

    first, _second = two_broadcasts
    _run(_journal(pool, first, 1, 2, 3))
    _run(_journal(pool, first, 2, 3))
    rows = _run(pool.fetchval(
        "SELECT count(*) FROM broadcast_delivery_log WHERE broadcast_id=$1", first))
    assert rows == 3, (
        f"в журнале {rows} строк на три доставки: ключ не держит, и счёт "
        "доставленных на докатке поехал бы")
    assert _run(db.get_broadcast_delivered_ids(pool, first)) == {1, 2, 3}


def test_one_broadcasts_journal_is_no_authority_for_another(pool, two_broadcasts):
    """Это и есть цена потерянного закрепления id: вторая рассылка — чистый лист."""
    from database import db

    first, second = two_broadcasts
    _run(_journal(pool, first, 1, 2, 3))
    assert _run(db.get_broadcast_delivered_ids(pool, second)) == set(), (
        "журнал первой рассылки виден второй — тогда и терять закрепление id "
        "было бы не страшно; на самом деле вторая пишет всем заново")
    assert _run(db.get_broadcast_delivered_ids(pool, first)) == {1, 2, 3}


def test_the_resume_skips_exactly_the_journalled_people(pool, two_broadcasts):
    """Так докатка и решает, кому не писать: разность аудитории и журнала."""
    from database import db

    first, _second = two_broadcasts
    audience = [1, 2, 3, 4, 5]
    _run(_journal(pool, first, 2, 4))
    already = _run(db.get_broadcast_delivered_ids(pool, first))
    assert [u for u in audience if u not in already] == [1, 3, 5], already
