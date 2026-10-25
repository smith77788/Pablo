"""Живой Postgres: удаление ботов и каналов/чатов исключает их отовсюду (v257).

Заглушка пула не проверяет сам SQL — CTE с DELETE внутри фильтра импорта,
приведение `$3::boolean` в паузе, приоритет AND/OR в условии видимости.
Здесь те же функции и запросы идут в настоящую базу.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 991853
BOT_ID = 5009853
_LOOP = None


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
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
    yield p
    _run(_clean(p))
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


async def _clean(pool):
    await pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT_ID)
    await pool.execute("DELETE FROM managed_channels WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM removed_channels WHERE owner_id=$1", OWNER)
    await pool.execute("DELETE FROM va_channel_admin WHERE owner_id=$1", OWNER)


@pytest.fixture(autouse=True)
def _fresh(pool):
    _run(_clean(pool))
    yield


def _visible_bots(pool):
    from services.mini_app_api import _BOTS_VISIBLE_SQL

    return _run(pool.fetch(
        f"SELECT mb.bot_id FROM managed_bots mb WHERE ({_BOTS_VISIBLE_SQL})", OWNER))


def _toggle(pool, want):
    return _run(pool.fetchrow(
        """UPDATE managed_bots
              SET is_active = COALESCE($3::boolean, NOT is_active)
            WHERE bot_id=$1 AND added_by=$2 AND deleted_at IS NULL
        RETURNING bot_id, is_active""", BOT_ID, OWNER, want))


def test_bot_pause_keeps_it_listed_delete_hides_it(pool):
    from database import db

    assert _run(db.add_bot(pool, "1:tok", BOT_ID, "u", "n", OWNER)) is True
    assert [r["bot_id"] for r in _visible_bots(pool)] == [BOT_ID]

    # пауза: в списке, но не в работе
    assert _toggle(pool, False)["is_active"] is False
    assert [r["bot_id"] for r in _visible_bots(pool)] == [BOT_ID]
    # явное состояние не переключает обратно при повторе
    assert _toggle(pool, False)["is_active"] is False

    # удаление: нет ни в списке, ни в работе, пауза не поднимает
    assert _run(db.delete_bot(pool, BOT_ID, OWNER)) is True
    assert _visible_bots(pool) == []
    assert _run(db.get_bots(pool, OWNER)) == []
    assert _toggle(pool, True) is None
    row = _run(pool.fetchrow(
        "SELECT is_active, deleted_at FROM managed_bots WHERE bot_id=$1", BOT_ID))
    assert row["is_active"] is False and row["deleted_at"] is not None

    # тот же токен — бот вернулся той же строкой
    assert _run(db.add_bot(pool, "1:tok", BOT_ID, "u", "n", OWNER)) is True
    assert [r["bot_id"] for r in _visible_bots(pool)] == [BOT_ID]


def test_dead_token_series_takes_bot_out_of_work(pool):
    from database import db
    from services import auto_responder as ar
    from services import bot_health as bh

    _run(db.add_bot(pool, "1:tok", BOT_ID, "u", "n", OWNER))
    for _ in range(bh.FAIL_STREAK_TO_ALERT - 1):
        _run(ar._record_bot_error(pool, BOT_ID, "Unauthorized", 401))
    assert _run(pool.fetchval(
        "SELECT is_active FROM managed_bots WHERE bot_id=$1", BOT_ID)) is True
    _run(ar._record_bot_error(pool, BOT_ID, "Unauthorized", 401))
    row = dict(_run(pool.fetchrow("SELECT * FROM managed_bots WHERE bot_id=$1", BOT_ID)))
    assert row["is_active"] is False
    assert bh.describe(row)["state"] == "dead"


def _add_channels(pool, *ids):
    from database import db

    return _run(db.add_managed_channels(pool, OWNER, 1, [
        {"id": i, "title": f"Ch{i}", "type": "channel", "access_hash": 7} for i in ids]))


def _channel_ids(pool):
    return sorted(r["channel_id"] for r in _run(pool.fetch(
        "SELECT channel_id FROM managed_channels WHERE owner_id=$1", OWNER)))


def test_deleted_channel_stays_deleted_through_import(pool):
    from database import db

    _add_channels(pool, 11, 12)
    _run(pool.execute(
        "INSERT INTO va_channel_admin(owner_id, channel_id, enabled) VALUES ($1, 12, TRUE)",
        OWNER))
    assert _run(db.remove_managed_channel(pool, OWNER, 12)) is True
    assert _channel_ids(pool) == [11]
    assert _run(pool.fetchval(
        "SELECT enabled FROM va_channel_admin WHERE owner_id=$1 AND channel_id=12",
        OWNER)) is False

    # оба вида импорта
    _add_channels(pool, 11, 12)
    assert _channel_ids(pool) == [11]
    _run(db.upsert_managed_channels(pool, OWNER, 1, [
        {"id": 11, "type": "channel"}, {"id": 12, "type": "channel"}], complete=True))
    assert _channel_ids(pool) == [11]

    removed = _run(db.list_removed_channels(pool, OWNER))
    assert [(r["channel_id"], r["reason"]) for r in removed] == [(12, "owner")]

    # вернуть — та же строка с тем же хешем
    assert _run(db.restore_removed_channel(pool, OWNER, 12)) is True
    assert _channel_ids(pool) == [11, 12]
    assert _run(pool.fetchval(
        "SELECT access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=12",
        OWNER)) == 7
    assert _run(db.list_removed_channels(pool, OWNER)) == []


def test_gone_channel_comes_back_when_seen_again(pool):
    from database import db

    _add_channels(pool, 21)
    assert _run(db.remove_managed_channel(
        pool, OWNER, 21, reason=db.REMOVED_GONE, detail="нет доступа")) is True
    assert _channel_ids(pool) == []
    _add_channels(pool, 21)
    assert _channel_ids(pool) == [21]
    assert _run(db.list_removed_channels(pool, OWNER)) == []


def test_explicit_add_forgets_the_removal(pool):
    from database import db

    _add_channels(pool, 31)
    _run(db.remove_managed_channel(pool, OWNER, 31))
    _run(db.forget_removed_channels(pool, OWNER, [31]))
    _add_channels(pool, 31)
    assert _channel_ids(pool) == [31]
