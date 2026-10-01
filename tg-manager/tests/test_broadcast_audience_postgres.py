"""Кого рассылка берёт в адресаты — проверено на живой базе.

ЗАЧЕМ ЖИВОЙ POSTGRES. Остальные тесты этой пары (`test_blocked_user_is_not_a_
recipient.py`, `test_suspect_subscribers_get_no_broadcast.py`) читают текст
запросов: они ловят пропавшее условие, но не могут поймать ни ошибку
связывания параметра, ни выражение, которое Postgres поймёт не так, как
задумано. А выражения тут как раз нетривиальные:

  * `block_user` пишет `is_blocked=$3::boolean, is_active = NOT $3::boolean` —
    один параметр в двух местах и с отрицанием;
  * `batch_upsert_users` в ветке ON CONFLICT ставит
    `is_active = NOT bot_users.is_blocked`, то есть читает СТАРОЕ значение
    строки в том же запросе, который её обновляет.

Заглушка пула не исполняет SQL и такие вещи не проверяет в принципе.

ЧТО ЗАКРЕПЛЯЕТСЯ. Адресат рассылки — тот, кто активен, не заблокировал бота и
не помечен накруткой. Заблокировавший выходит из аудитории сразу и возвращается
только через Telegram; человек, однажды помеченный «неактивен» по недоставке,
возвращается, когда снова пишет боту, — но блокировку это не снимает.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py; переменная та
же, INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991802
BOT = 7718020001
TOKEN = "7718020001:test-audience-fixture"

NORMAL, BLOCKED, SUSPECT, GONE = 50001, 50002, 50003, 50004

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

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(p.close())


@pytest.fixture(autouse=True)
def audience(pool):
    """Четыре подписчика: обычный, заблокировавший, накрученный и ушедший."""
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(pool.execute(
        "INSERT INTO managed_bots(token, bot_id, username, added_by) "
        "VALUES($1,$2,'audience_fixture',$3)", TOKEN, BOT, OWNER))
    _run(pool.executemany(
        "INSERT INTO bot_users(bot_id, user_id, language_code, is_active, "
        "is_blocked, suspect) VALUES($1,$2,'ru',$3,$4,$5)",
        [(BOT, NORMAL, True, False, False),
         (BOT, BLOCKED, True, True, False),
         (BOT, SUSPECT, True, False, True),
         (BOT, GONE, False, False, False)]))
    yield
    _run(pool.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))


def _flags(pool, user_id: int) -> dict:
    row = _run(pool.fetchrow(
        "SELECT is_active, is_blocked FROM bot_users WHERE bot_id=$1 AND user_id=$2",
        BOT, user_id))
    return dict(row) if row else {}


# ── кого берут выборки адресатов ─────────────────────────────────────────────

def test_only_the_reachable_subscriber_is_a_recipient(pool):
    from database import db

    ids = _run(db.get_audience_user_ids(pool, BOT))
    assert ids == [NORMAL], (
        f"в адресатах оказались лишние: {sorted(ids)}; "
        f"{BLOCKED} заблокировал бота, {SUSPECT} помечен накруткой, "
        f"{GONE} выведен из аудитории")


def test_language_segment_is_a_recipient_list_too(pool):
    from database import db

    ids = _run(db.get_audience_by_language(pool, BOT, "ru"))
    assert ids == [NORMAL], f"сегмент по языку берёт лишних: {sorted(ids)}"


def test_new_users_segment_is_a_recipient_list_too(pool):
    """Все четверо заведены только что, то есть попадают в окно «за 7 дней»."""
    from database import db

    ids = _run(db.get_audience_new_users(pool, BOT, 7))
    assert ids == [NORMAL], f"сегмент «новые» берёт лишних: {sorted(ids)}"


def test_audience_count_matches_the_recipient_list(pool):
    """Счётчик на экране и список адресатов должны сойтись."""
    from database import db

    ids = _run(db.get_audience_user_ids(pool, BOT))
    count = _run(db.get_audience_count(pool, BOT))
    assert count == len(ids), (
        f"экран обещает {count}, а уйдёт {len(ids)}")


# ── флаги: оба параметра одного запроса ──────────────────────────────────────

def test_block_removes_and_return_restores(pool):
    from database import db

    _run(db.block_user(pool, BOT, NORMAL, True))
    assert _flags(pool, NORMAL) == {"is_active": False, "is_blocked": True}
    assert _run(db.get_audience_user_ids(pool, BOT)) == [], (
        "заблокировавший остался адресатом")

    _run(db.block_user(pool, BOT, NORMAL, False))
    assert _flags(pool, NORMAL) == {"is_active": True, "is_blocked": False}
    assert _run(db.get_audience_user_ids(pool, BOT)) == [NORMAL], (
        "вернувшийся из блокировки не вернулся в аудиторию")


def test_delivery_failure_marks_a_block_as_a_block(pool):
    from database import db

    _run(db.mark_user_inactive(pool, BOT, NORMAL, reason="blocked"))
    assert _flags(pool, NORMAL) == {"is_active": False, "is_blocked": True}

    _run(db.mark_user_inactive(pool, BOT, SUSPECT, reason="deactivated"))
    assert _flags(pool, SUSPECT)["is_blocked"] is False, (
        "удалённый аккаунт выдан за заблокировавшего")


def test_writing_again_brings_back_only_those_who_are_not_blocked(pool):
    """Ветка ON CONFLICT читает старое значение is_blocked той же строки."""
    from database import db

    _run(db.upsert_users(pool, BOT, [
        {"user_id": GONE, "first_name": "Вернулся"},
        {"user_id": BLOCKED, "first_name": "Заблокировал"},
    ]))
    assert _flags(pool, GONE) == {"is_active": True, "is_blocked": False}, (
        "однажды помеченный неактивным не вернулся, хотя снова пишет боту")
    assert _flags(pool, BLOCKED) == {"is_active": False, "is_blocked": True}, (
        "старый апдейт из хвоста getUpdates воскресил заблокировавшего")

    assert sorted(_run(db.get_audience_user_ids(pool, BOT))) == [NORMAL, GONE], (
        "вернувшийся не попал в адресаты")
